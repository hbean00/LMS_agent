"""여러 에이전트 CLI 연결(계약 21장): 실행기 목록·도움말 확인(probe)·stdin만·환경 변수 빼기·출력 읽기·고르기(auto 순서,
custom은 자동 선택 안 함)·화면 요청(/api/ai/provider·/api/ai/test·/api/ai/rescan)·CLI(ai-eval --provider, ai-providers)·
claude 호출이 예전과 같은지. 실제 CLI는 띄우지 않는다(가짜 도움말·가짜 실행기)."""
import json
import subprocess
import threading

import pytest

from lms_agent import agent_cli, cli, config
from lms_agent.store import Store
from test_agent_cli import CMD, FakeCLI, eval_oracle

REAL_HELP_TEXT = agent_cli._help_text      # conftest가 막기 전의 진짜 함수(환경·stdin 검사용)
REAL_FIND = agent_cli.find_exe

CODEX_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Arguments:
  [PROMPT]
          Initial instructions for the agent. If not provided as an argument (or if `-` is used),
          instructions are read from stdin. If stdin is piped and a prompt is also provided, stdin
          is appended as a `<stdin>` block

Options:
  -s, --sandbox <SANDBOX_MODE>
          Select the sandbox policy to use when executing model-generated shell commands
          [possible values: read-only, workspace-write, danger-full-access]
      --skip-git-repo-check
          Allow running Codex outside a Git repository
      --ephemeral
          Run without persisting session files to disk
      --color <COLOR>
          [default: auto]
          [possible values: always, never, auto]
      --json
          Print events to stdout as JSONL
"""
GEMINI_HELP = """Usage: gemini [options] [command]

Options:
  -m, --model          Model  [string]
  -p, --prompt         Prompt. Appended to input on stdin (if any).  [string]
  -o, --output-format  The format of the CLI output.  [string] [choices: "text", "json", "stream-json"]
      --approval-mode  Set the approval mode: default (prompt for approval), auto_edit (auto-approve edit tools),
                       yolo (auto-approve all tools)  [string] [choices: "default", "auto_edit", "yolo"]
  -y, --yolo           Automatically accept all actions  [boolean] [default: false]
"""
COPILOT_HELP = """Usage: copilot [options]

Options:
  -p, --prompt <text>        Execute a prompt in non-interactive mode; standard input (stdin) is appended
  --allow-all-tools          Allow all tools to run automatically without confirmation
  --deny-tool [tools...]     Tools the CLI does not have permission to use
  --no-color                 Disable all color output
  -s, --silent               Output only the agent response (no stats)
"""
OPENCODE_HELP = """opencode run [message..]

run opencode with a message; reads stdin when piped

Options:
  -m, --model   model to use in the format of provider/model  [string]
      --format  format: default (formatted) or json (raw JSON events)  [string] [choices: "default", "json"]
"""
OLLAMA_RUN_HELP = """Run a model

Usage:
  ollama run MODEL [PROMPT] [flags]

Flags:
      --format string      Response format (e.g. json)
  -h, --help               help for run
      --hidethinking       Hide thinking output (if provided)
      --nowordwrap         Don't wrap words to the next line automatically
"""
OLLAMA_LIST = """NAME               ID              SIZE      MODIFIED
llama3.2:3b        a80c4f17acd5    2.0 GB    2 weeks ago
qwen3:8b           500a1f067a9f    5.2 GB    3 days ago
"""
HELPS = {"codex": CODEX_HELP, "gemini": GEMINI_HELP, "copilot": COPILOT_HELP, "opencode": OPENCODE_HELP,
         "ollama": OLLAMA_RUN_HELP}
TEXT = "LAB4 보고서 마감은 11월 18일 자정입니다 — 비밀강의글XYZ"


def fake_help(texts=None, version="9.8.7", listing=OLLAMA_LIST, calls=None):
    """가짜 도움말: 실행 파일 이름(경로 끝)으로 어느 CLI인지 보고 그 도움말을 돌려준다."""
    texts = {**HELPS, **(texts or {})}

    def run(args):
        if calls is not None:
            calls.append(list(args))
        name = agent_cli.command_name(args[0])
        rest = list(args[1:])
        if rest == ["--version"]:
            return 0, f"{name} version {version}"
        if rest == ["list"]:
            return 0, listing
        return 0, texts.get(name, "")
    return run


def finder(found):
    """가짜 PATH: found에 있는 실행기만 'C:\\Users\\someone\\bin\\<이름>.cmd'로 찾은 것처럼."""
    def find(adapter):
        return rf"C:\Users\someone\bin\{adapter.exe_names[0]}.cmd" if adapter.id in found else None
    return find


ALL = ("claude", "codex", "gemini", "opencode", "copilot", "ollama")


# ---------------------------------------------------------------- 목록·도움말 확인(probe)

def test_registry_has_spec_adapters_and_shapes():
    assert set(agent_cli.ADAPTERS) == {"claude", "codex", "gemini", "opencode", "copilot", "ollama", "custom"}
    assert agent_cli.AUTO_ORDER == ("claude", "codex", "gemini", "opencode", "copilot", "ollama")
    for a in agent_cli.ADAPTERS.values():
        assert a.output in agent_cli.OUTPUTS and a.prompt_mode in ("stdin", "arg")
        assert all(f.role in ("run", "safe", "extra") for f in a.flags)
        assert all("{prompt}" not in x or a.prompt_mode == "arg" for x in a.args)
    assert agent_cli.ADAPTERS["claude"].no_tools_flags == ["--tools", ""]
    assert agent_cli.ADAPTERS["codex"].no_tools_flags == ["--sandbox", "read-only"]
    assert agent_cli.ADAPTERS["ollama"].local and agent_cli.ADAPTERS["ollama"].no_tools


def test_probe_enables_only_flags_present_in_help():
    r = agent_cli.probe("codex", r"C:\Users\someone\bin\codex.cmd", help_runner=fake_help())
    assert r["status"] == "ready" and r["safe"] and r["version"] == "9.8.7" and r["output"] == "json-lines"
    assert r["args"] == ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never",
                         "--json"]
    assert "--sandbox read-only" in r["confirmed"] and not r["missing"]
    # 도움말에 없는 옵션은 붙이지 않는다: --json·--ephemeral이 없는 옛 버전 → 그 옵션 없이, 출력은 글 그대로
    old = CODEX_HELP.replace("--json", "").replace("--ephemeral", "")
    r = agent_cli.probe("codex", "codex", help_runner=fake_help({"codex": old}))
    assert r["status"] == "ready" and "--json" not in r["args"] and "--ephemeral" not in r["args"] and r["output"] == "text"


@pytest.mark.parametrize("aid, cut, what", [
    ("codex", "--sandbox", "읽기 전용"),                 # 도구 끄기(샌드박스) 옵션이 없으면 안전하지 않음
    ("codex", "stdin", "표준 입력"),                     # 글을 stdin으로 받는지 모르면 쓰지 않음
    ("gemini", "--approval-mode", "셸"),
    ("copilot", "--deny-tool", "셸"),
    ("copilot", "stdin", "표준 입력"),
])
def test_probe_missing_required_flag_is_needs_check(aid, cut, what):
    text = HELPS[aid].replace(cut, "")
    if cut == "stdin":
        text = text.replace("standard input", "")
    r = agent_cli.probe(aid, aid, help_runner=fake_help({aid: text}))
    assert r["status"] == "needs_check" and any(what in m for m in r["missing"]), r
    assert r["note"].startswith("도움말에서 확인하지 못한 것")


def test_probe_other_adapters():
    g = agent_cli.probe("gemini", "gemini", help_runner=fake_help())
    assert g["status"] == "ready" and g["output"] == "json" and g["args"] == ["--approval-mode", "default",
                                                                            "--output-format", "json"]
    c = agent_cli.probe("copilot", "copilot", help_runner=fake_help())
    assert c["status"] == "ready" and c["args"][:2] == ["-p", "{prompt}"]
    assert ["--deny-tool", "shell", "--deny-tool", "write"] == c["args"][2:6] and "--allow-all-tools" not in c["args"]
    o = agent_cli.probe("opencode", "opencode", help_runner=fake_help())   # 도구를 끄는 옵션이 알려지지 않음
    assert o["status"] == "needs_check" and not o["safe"] and any("도구를 끄는" in m for m in o["missing"])


def test_probe_ollama_models_and_privacy(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    r = agent_cli.probe("ollama", "ollama", help_runner=fake_help())
    assert r["status"] == "ready" and r["safe"] and r["local"] and r["model"] == "llama3.2:3b"
    assert r["models"] == ["llama3.2:3b", "qwen3:8b"]
    assert r["args"] == ["run", "llama3.2:3b", "--hidethinking", "--nowordwrap", "--format", "json"]
    assert agent_cli.probe("ollama", "ollama", help_runner=fake_help(), model="qwen3:8b")["args"][1] == "qwen3:8b"
    # 받지 않은 모델은 쓰지 않는다(ollama run이 몇 GB를 받기 시작하지 않게)
    r = agent_cli.probe("ollama", "ollama", help_runner=fake_help(), model="mistral:7b")
    assert r["status"] == "needs_check" and any("ollama pull mistral:7b" in m for m in r["missing"])
    r = agent_cli.probe("ollama", "ollama", help_runner=fake_help(listing="Error: could not connect to ollama app"))
    assert r["status"] == "needs_check" and r["model"] is None and r["models"] == []
    cloud = agent_cli.probe("ollama", "ollama", help_runner=fake_help(listing=OLLAMA_LIST + "gpt-oss:120b-cloud  "
                                                                     "0123456789ab    -    1 day ago\n"),
                            model="gpt-oss:120b-cloud")
    assert cloud["status"] == "ready" and not cloud["local"]          # 클라우드 모델은 글이 PC 밖으로 간다
    monkeypatch.setenv("OLLAMA_HOST", "http://192.168.0.7:11434")
    assert not agent_cli.probe("ollama", "ollama", help_runner=fake_help())["local"]
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    assert agent_cli.probe("ollama", "ollama", help_runner=fake_help())["local"]


def test_probe_timeout_or_missing_exe_is_needs_check_without_path():
    def slow(args):
        raise subprocess.TimeoutExpired(args, 10)

    def gone(args):
        raise FileNotFoundError(2, "No such file", r"C:\Users\someone\bin\codex.cmd")
    for run in (slow, gone):
        r = agent_cli.probe("codex", r"C:\Users\someone\bin\codex.cmd", help_runner=run)
        assert r["status"] == "needs_check" and r["note"] and "someone" not in json.dumps(r, ensure_ascii=False)


def test_probe_cache_reuses_result_until_cleared(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_cli, "_help_text", lambda args, timeout=None: fake_help(calls=calls)(args))
    a = agent_cli.cached_probe("codex", "codex")
    b = agent_cli.cached_probe("codex", "codex")
    assert a is b and len(calls) == 2                       # 도움말 + --version 한 번씩
    assert agent_cli.cached_probe("gemini", "gemini", run=False) is None
    agent_cli.clear_probe_cache()
    agent_cli.cached_probe("codex", "codex")
    assert len(calls) == 4


def test_help_text_runs_without_shell_stdin_or_our_env(monkeypatch):
    seen = {}

    class P:
        returncode = 0
        pid = 1

        def __init__(self, args, **kw):
            seen.update(kw, args=args)

        def communicate(self, timeout=None):
            return "usage --json", ""
    monkeypatch.setenv("LMS_AGENT_TOKEN", "tok")
    monkeypatch.setattr(agent_cli.subprocess, "Popen", P)
    code, text = REAL_HELP_TEXT(["codex", "exec", "--help"])
    assert code == 0 and "--json" in text
    assert seen["stdin"] is subprocess.DEVNULL and "shell" not in seen and seen["cwd"]
    assert "LMS_AGENT_TOKEN" not in seen["env"] and "PATH" in {k.upper() for k in seen["env"]}


# ---------------------------------------------------------------- 환경 변수·stdin만

def test_child_env_strips_ours_and_keeps_cli_credentials(monkeypatch):
    monkeypatch.setenv("LMS_AGENT_TOKEN", "canvas-secret")
    monkeypatch.setenv("LMS_AGENT_GOOGLE_CLIENT_SECRET", "g-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "g2")
    monkeypatch.setenv("GOOGLE_API_KEY", "user-gemini-key")
    monkeypatch.setenv("MY_APP_SECRET_KEY", "from-our-dotenv")
    monkeypatch.setenv("OPENAI_API_KEY", "user-openai-key")
    monkeypatch.setenv("HOME", "/home/x")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\x")
    monkeypatch.setattr(config, "DOTENV_KEYS", {"MY_APP_SECRET_KEY"})
    env = {k.upper(): v for k, v in agent_cli.child_env().items()}
    for gone in ("LMS_AGENT_TOKEN", "LMS_AGENT_GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET", "GOOGLE_API_KEY",
                 "MY_APP_SECRET_KEY"):
        assert gone not in env
    for kept in ("PATH", "HOME", "USERPROFILE", "OPENAI_API_KEY"):
        assert kept in env
    gem = {k.upper() for k in agent_cli.child_env(agent_cli.ADAPTERS["gemini"].own_env)}
    assert "GOOGLE_API_KEY" in gem and "GOOGLE_CLIENT_SECRET" not in gem   # gemini는 자기 인증 키만 남긴다
    monkeypatch.setattr(config, "DOTENV_KEYS", {"GOOGLE_API_KEY"})         # 우리 .env가 넣은 것이면 그래도 뺀다
    assert "GOOGLE_API_KEY" not in {k.upper() for k in agent_cli.child_env(agent_cli.ADAPTERS["gemini"].own_env)}


def test_dotenv_loader_records_names(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOTENV_KEYS", set())
    monkeypatch.delenv("LMS_TEST_DOTENV_SECRET", raising=False)
    (tmp_path / ".env").write_text("LMS_TEST_DOTENV_SECRET=abc\n", "utf-8")
    config.load_dotenv(tmp_path / ".env")
    assert "LMS_TEST_DOTENV_SECRET" in config.DOTENV_KEYS
    assert "LMS_TEST_DOTENV_SECRET" not in agent_cli.child_env()


def plans():
    """도움말이 다 있는 PC에서 실행기마다 만든 Plan(claude·custom 포함)."""
    out = {}
    s = {"ai_custom": {"path": "mycli", "args": ["--read-only"], "output": "text"}}
    for aid in ALL:
        e = agent_cli._entry(aid, s, None, True, True, fake_help(), finder(ALL))
        out[aid] = e["_plan"] if e.get("_plan") else None
    return out


def test_every_ready_adapter_sends_text_only_on_stdin(monkeypatch):
    monkeypatch.setattr(agent_cli.shutil, "which", lambda n: r"C:\tools\mycli.exe" if n == "mycli" else None)
    ps = plans()
    ps["custom"] = agent_cli._entry("custom", {"ai_custom": {"path": "mycli", "args": ["--read-only", "{prompt}"]}},
                                    None, True, False)["_plan"]
    assert ps["opencode"] is None and all(ps[i] for i in ("claude", "codex", "gemini", "copilot", "ollama", "custom"))
    for aid, plan in ps.items():
        if plan is None:
            continue
        fake = FakeCLI("{}")
        agent_cli.run_task("deadline_from_text", {"title": "공지", "text": TEXT}, runner=fake, provider=plan)
        call = fake.calls[0]
        argv = " ".join(call["args"])
        assert "비밀강의글XYZ" not in argv and "INPUT START" not in argv and "LAB4" not in argv, aid
        assert "비밀강의글XYZ" in call["input"] and "INPUT START" in call["input"], aid
        extra = [a for a in call["args"][1:] if a not in plan.variants[0]]
        if plan.prompt_mode == "arg" or aid == "custom":   # 인자로 갈 수 있는 것은 고정된 한 줄 지시뿐
            assert extra == [agent_cli._safe_prompt(agent_cli.PROMPT, plan.exe)], aid
            assert agent_cli.PROMPT not in call["input"] or aid == "custom"
        else:
            assert extra == [] and call["input"].startswith(agent_cli.PROMPT), aid
        assert call["cwd"] and call["timeout"] == 90


def test_default_runner_gets_stripped_env_and_stdin(monkeypatch):
    seen = {}

    class P:
        returncode = 0
        pid = 1

        def __init__(self, args, **kw):
            seen.update(kw, args=args)

        def communicate(self, input=None, timeout=None):
            seen["input"] = input
            return json.dumps({"response": json.dumps({"deadlines": []}), "stats": {"models": {"gemini-x": {}}}}), ""
    monkeypatch.setenv("LMS_AGENT_TOKEN", "canvas-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "user-gemini-key")
    monkeypatch.setattr(agent_cli.subprocess, "Popen", P)
    plan = plans()["gemini"]
    res = agent_cli.run_task("deadline_from_text", {"text": TEXT}, provider=plan)
    assert res.provider == "gemini" and res.cli == "gemini" and res.model == "gemini-x"
    assert "비밀강의글XYZ" in seen["input"] and "비밀강의글XYZ" not in " ".join(seen["args"])
    env = {k.upper() for k in seen["env"]}
    assert "LMS_AGENT_TOKEN" not in env and "GOOGLE_API_KEY" in env and "shell" not in seen


# ---------------------------------------------------------------- 출력 읽기

WEIGHTS = {"components": [{"name": "시험", "weight": 100}], "evidence": "시험 100%"}


def test_output_formats():
    js = json.dumps(WEIGHTS, ensure_ascii=False)
    # claude-json(예전 그대로)
    claude = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "답: " + js,
                         "modelUsage": {"claude-x": {}}})
    assert agent_cli.parse_output("claude-json", claude) == (WEIGHTS, {"model": "claude-x"})
    # json: gemini 모양(response + stats) · 객체 자체가 답 · 오류만
    gem = json.dumps({"response": "```json\n" + js + "\n```", "stats": {"models": {"gemini-2.5-pro": {}}}})
    assert agent_cli.parse_output("json", gem) == (WEIGHTS, {"model": "gemini-2.5-pro"})
    assist = {"answer": "이렇게 찾았어요", "proposals": []}
    assert agent_cli.parse_output("json", json.dumps(assist, ensure_ascii=False))[0] == assist
    with pytest.raises(agent_cli.AgentCliError, match="오류"):
        agent_cli.parse_output("json", json.dumps({"error": {"type": "auth", "message": "로그인 필요"}}))
    # json-lines: codex 새 모양(생각 과정은 건너뜀)·옛 모양·opencode 모양·오류만
    lines = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "0", "type": "reasoning", "text": "생각 {\"x\": 1}"}},
        {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": js}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}}])
    assert agent_cli.parse_output("json-lines", lines)[0] == WEIGHTS
    old = json.dumps({"id": "1", "msg": {"type": "agent_message", "message": js}}, ensure_ascii=False)
    assert agent_cli.parse_output("json-lines", "garbage line\n" + old)[0] == WEIGHTS
    oc = json.dumps({"type": "text", "part": {"type": "text", "text": js}}, ensure_ascii=False)
    assert agent_cli.parse_output("json-lines", oc)[0] == WEIGHTS
    with pytest.raises(agent_cli.AgentCliError, match="quota"):
        agent_cli.parse_output("json-lines", json.dumps({"type": "turn.failed", "error": {"message": "quota exceeded"}}))
    # text: 색 코드·생각 과정·말머리를 지나 첫 JSON 객체
    text = "\x1b[1mThinking...\n{bad}\n...done thinking.\x1b[0m\n\n결과입니다:\n" + js + "\n끝"
    assert agent_cli.parse_output("text", text) == (WEIGHTS, {})
    assert agent_cli.parse_output("text", "<think>{\"no\": 1}</think> " + js)[0] == WEIGHTS
    assert agent_cli.parse_output("text", "모르겠어요") == (None, {})


# ---------------------------------------------------------------- 고르기

def test_auto_picks_first_ready_and_safe_in_order():
    def pick(found, helps=None, settings=None):
        plan, why = agent_cli.select(settings or {}, help_runner=fake_help(helps), finder=finder(found))
        return (plan.id if plan else None), why
    assert pick(ALL) == ("claude", None)
    assert pick(("codex", "gemini", "ollama")) == ("codex", None)
    no_sandbox = {"codex": CODEX_HELP.replace("--sandbox", "")}
    assert pick(("codex", "gemini"), no_sandbox) == ("gemini", None)       # 확인 필요는 건너뛴다
    assert pick(("opencode", "ollama")) == ("ollama", None)                 # opencode는 확인 필요
    plan, why = agent_cli.select({}, help_runner=fake_help(), finder=finder(()))
    assert plan is None and why == agent_cli.NOT_FOUND and "찾지 못했습니다" in why


def test_custom_is_never_auto_selected_but_usable_when_chosen(tmp_path):
    exe = tmp_path / "mycli.exe"
    exe.write_text("")
    s = {"ai_custom": {"path": str(exe), "args": ["--no-tools"], "output": "json-lines"}}
    plan, why = agent_cli.select(s, help_runner=fake_help(), finder=finder(()))
    assert plan is None and why == agent_cli.NOT_FOUND
    plan, why = agent_cli.select({**s, "ai_provider": "custom"}, help_runner=fake_help(), finder=finder(()))
    assert plan.id == "custom" and not plan.safe and plan.output == "json-lines" and plan.variants == (("--no-tools",),)
    ents = agent_cli.providers(s, help_runner=fake_help(), finder=finder(()))
    cu = next(e for e in ents if e["id"] == "custom")
    assert cu["status"] == "ready" and not cu["safe"] and not cu["auto"] and cu["warning"] == agent_cli.CUSTOM_WARNING
    assert agent_cli.pick(ents, s) is None and agent_cli.pick(ents, s, "custom")["id"] == "custom"


def test_explicit_choice_never_falls_back_silently():
    s = {"ai_provider": "gemini"}
    plan, why = agent_cli.select(s, help_runner=fake_help(), finder=finder(("claude", "codex")))
    assert plan is None and "Gemini CLI" in why and "찾지 못했습니다" in why
    plan, why = agent_cli.select({"ai_provider": "codex"}, help_runner=fake_help({"codex": "no flags here"}),
                                 finder=finder(ALL))
    assert plan is None and "확인하지 못해" in why
    assert agent_cli.select({"ai_provider": "nope"})[0] is None
    # 데모·테스트처럼 claude를 넣어 주면 PATH를 보지 않는다: codex를 골랐으면 실행하지 않고 오류
    fake = FakeCLI("{}")
    store_settings = {"ai_provider": "codex"}

    class S:
        data = {"settings": store_settings}
    plan, why = agent_cli.resolve(None, CMD, S())
    assert plan is None and "찾지 못했습니다" in why and not fake.calls


def test_providers_public_entries_have_no_paths():
    ents = agent_cli.providers({}, help_runner=fake_help(), finder=finder(ALL), probe_now=True)
    dumped = json.dumps([agent_cli.public(e) for e in ents], ensure_ascii=False)
    assert "someone" not in dumped and "_plan" not in dumped and ".cmd" not in dumped
    by = {e["id"]: e for e in ents}
    assert by["codex"]["path_hint"] == "codex" and by["codex"]["status"] == "ready" and by["codex"]["version"] == "9.8.7"
    assert by["opencode"]["status"] == "needs_check" and by["custom"]["status"] == "missing"
    unprobed = agent_cli.providers({}, finder=finder(("codex",)))
    cx = next(e for e in unprobed if e["id"] == "codex")
    assert cx["found"] and cx["status"] == "needs_check" and not cx["probed"]   # 확인 전에는 띄우지 않는다


# ---------------------------------------------------------------- claude는 예전과 같다

def test_claude_invocation_unchanged():
    p = agent_cli.claude_plan(CMD)
    line = agent_cli._safe_prompt(agent_cli.PROMPT, CMD)
    assert p.argv(agent_cli.PROMPT) == [
        [CMD, "-p", line, "--output-format", "json", "--tools", "", "--no-session-persistence"],
        [CMD, "-p", line, "--output-format", "json"]]
    assert p.output == "claude-json" and p.safe and p.prompt_mode == "arg"
    fake = FakeCLI({"deadlines": []})
    res = agent_cli.run_task("deadline_from_text", {"text": TEXT}, runner=fake, command=CMD)
    assert res.ok and res.provider == "claude" and res.cli == "claude"
    assert not fake.calls[0]["input"].startswith(agent_cli.PROMPT)    # claude는 고정 지시를 -p로(예전대로)
    s = Store.__new__(Store)
    s.data = {"settings": {"ai_provider": "claude"}}
    assert agent_cli.resolve(None, CMD, s)[0].id == "claude"


def test_session_uses_given_provider(tmp_path):
    store = Store(tmp_path / "state.json")
    store.data["settings"]["ai_cli"] = True
    plan = plans()["codex"]
    fake = FakeCLI("{\"deadlines\": []}")
    s = agent_cli.AiSession(store, runner=fake, provider=plan)
    res = s.run("deadline_from_text", {"text": TEXT})
    assert res.ok and fake.calls[0]["args"][1] == "exec" and store.data["ai_runs"][-1]["cli"] == "codex"
    assert set(store.data["ai_runs"][-1]) == {"at", "task", "ok", "attempts", "duration_ms", "input_chars", "cli",
                                              "model", "error"}


# ---------------------------------------------------------------- 화면 요청(features/ai.py)

@pytest.fixture()
def app(tmp_path):
    from lms_agent.server import App
    s = Store(tmp_path / "state.json")
    from lms_agent.features import ai as ai_feature
    a = App(s, tmp_path, cfg={}, ai_runner=FakeCLI(eval_oracle), ai_command=CMD)
    a.state = lambda: {"ai": ai_feature.ai_state(a, None)}   # 다른 기능의 상태 칸과 떼어 이 기능만 본다
    return a


def call(app, path, body):
    from lms_agent.api import router
    for r, groups in router.match("POST", path):
        out = r.handler(app, body, **groups)
        if out is not None:
            return out


def test_state_lists_providers_names_only(app):
    from lms_agent.features import ai as ai_feature
    st = ai_feature.ai_state(app, None)
    by = {p["id"]: p for p in st["providers"]}
    assert st["provider"] == "claude" and st["selected"] == "auto" and st["available"] and st["command"] == "claude"
    assert by["claude"]["status"] == "ready" and by["claude"]["safe"] and by["claude"]["path_hint"] == "claude"
    assert all(by[i]["status"] == "missing" for i in ("codex", "gemini", "opencode", "copilot", "ollama"))
    assert by["custom"]["status"] == "missing" and st["custom"]["set"] is False and st["test"] is None
    assert st["test_cases"] == len(agent_cli.EVAL_CASES) and not st["local"]
    assert "someone" not in json.dumps(st, ensure_ascii=False)
    agent_st = __import__("lms_agent.features.agent", fromlist=["agent_state"]).agent_state(app, None)
    assert agent_st["available"] is True


def test_provider_route_validates_and_switches(app, tmp_path):
    from lms_agent.api import ApiError
    st = app.store.data["settings"]
    with pytest.raises(ApiError) as e:
        call(app, "/api/ai/provider", {"provider": "codex"})     # 없음 → 저장하지 않음
    assert e.value.status == 400 and "찾지 못했습니다" in str(e.value) and "ai_provider" not in st
    with pytest.raises(ApiError):
        call(app, "/api/ai/provider", {"provider": "rm -rf"})
    with pytest.raises(ApiError):
        call(app, "/api/ai/provider", {"provider": "custom"})    # 아직 지정 안 함
    exe = tmp_path / "Users" / "someone" / "mycli.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    for bad in ({"path": str(exe), "stdin": False}, {"path": str(exe), "output": "xml"},
                {"path": str(tmp_path / "nope.exe")}, {"path": str(exe), "args": ["a\nb"]}):
        with pytest.raises(ApiError):
            call(app, "/api/ai/provider", {"provider": "custom", "custom": bad})
    assert "ai_custom" not in st
    bat = tmp_path / "x.cmd"
    bat.write_text("")
    with pytest.raises(ApiError) as e:
        call(app, "/api/ai/provider", {"provider": "custom", "custom": {"path": str(bat), "args": ["a&b"]}})
    assert ".cmd" in str(e.value)
    out = call(app, "/api/ai/provider", {"provider": "custom",
                                         "custom": {"path": str(exe), "args": ["--no-tools", '""', ""], "output": "text"}})
    assert st["ai_provider"] == "custom" and st["ai_custom"] == {"path": str(exe), "args": ["--no-tools", ""],
                                                                 "stdin": True, "output": "text"}
    ai = out["ai"]
    assert ai["provider"] == "custom" and ai["custom"]["name"] == "mycli" and ai["custom"]["args"] == ["--no-tools", ""]
    assert str(exe) not in json.dumps(ai, ensure_ascii=False) and "someone" not in json.dumps(ai, ensure_ascii=False)
    # 고른 실행기를 동기화·정리가 그대로 쓴다(settings → resolve)
    fake = FakeCLI("{\"deadlines\": []}")
    res = agent_cli.run_task("deadline_from_text", {"text": TEXT}, runner=fake, command=app.ai_command, store=app.store)
    assert res.provider == "custom" and fake.calls[0]["args"] == [str(exe), "--no-tools", ""]
    # 경로를 비워 보내면 예전 경로 그대로(화면은 경로를 다시 보여 주지 않으므로)
    call(app, "/api/ai/provider", {"custom": {"args": ["--x"]}})
    assert st["ai_custom"]["path"] == str(exe) and st["ai_custom"]["args"] == ["--x"]
    with pytest.raises(ApiError):
        call(app, "/api/ai/provider", {"model": "bad model!"})
    call(app, "/api/ai/provider", {"model": "llama3.2:3b"})
    assert st["ai_model"] == "llama3.2:3b"
    out = call(app, "/api/ai/provider", {"provider": "auto", "model": None})
    assert out["ai"]["provider"] == "claude" and "ai_model" not in st and st["ai_provider"] == "auto"
    call(app, "/api/ai/provider", {"custom": None})
    assert "ai_custom" not in st


def test_connection_test_runs_in_background(app):
    from lms_agent.api import ApiError
    from lms_agent.features import ai as ai_feature
    out = call(app, "/api/ai/test", {})
    assert out["job"] and out["ai"]["test"]["status"] in ("running", "done")
    assert ai_feature.wait_test(app, 30)
    t = ai_feature.ai_state(app, None)["test"]
    assert t["status"] == "done" and t["provider"] == "claude" and t["passed"] == t["total"] == len(agent_cli.EVAL_CASES)
    assert t["done"] == t["total"] and all(c["label"] and c["passed"] for c in t["cases"])
    assert not app.store.data.get("ai_runs")                   # 시험은 호출 한도·기록에 들어가지 않는다
    # 한 번에 하나만
    gate = threading.Event()

    def slow(args, input, timeout, cwd):
        gate.wait(10)
        return FakeCLI(eval_oracle)(args, input, timeout, cwd)
    app.ai_runner = slow
    call(app, "/api/ai/test", {"provider": "claude"})
    with pytest.raises(ApiError) as e:
        call(app, "/api/ai/test", {})
    assert e.value.status == 409
    gate.set()
    assert ai_feature.wait_test(app, 30)
    with pytest.raises(ApiError) as e:
        call(app, "/api/ai/test", {"provider": "ollama"})      # 없는 실행기
    assert e.value.status == 400
    assert call(app, "/api/ai/rescan", {})["ai"]["provider"] == "claude"


# ---------------------------------------------------------------- CLI

def codex_runner(record):
    def run(args, input, timeout, cwd, env=None):
        record.append({"args": list(args), "input": input, "env": env})
        ans = eval_oracle(args, input)
        text = json.dumps(ans, ensure_ascii=False) if isinstance(ans, dict) else ans
        events = [{"type": "thread.started", "thread_id": "t"},
                  {"type": "item.completed", "item": {"id": "0", "type": "reasoning", "text": "생각 중"}},
                  {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {}}]
        return subprocess.CompletedProcess(args, 0, "\n".join(json.dumps(x, ensure_ascii=False) for x in events), "")
    return run


def test_cli_ai_eval_with_provider_and_ai_providers(monkeypatch, capsys):
    record = []
    monkeypatch.setattr(agent_cli, "find_exe", finder(("codex",)))
    monkeypatch.setattr(agent_cli, "_help_text", lambda args, timeout=None: fake_help()(args))
    monkeypatch.setattr(agent_cli, "_default_runner", codex_runner(record))
    monkeypatch.setenv("LMS_AGENT_TOKEN", "canvas-secret")
    with pytest.raises(SystemExit) as e:
        cli.main(["ai-eval", "--provider", "codex"])
    out = capsys.readouterr().out
    assert e.value.code == 0, out
    assert f"codex: {len(agent_cli.EVAL_CASES)}/{len(agent_cli.EVAL_CASES)} 통과" in out and "someone" not in out
    first = record[0]
    assert first["args"][1:3] == ["exec", "--sandbox"] and "INPUT START" in first["input"]
    assert "INPUT START" not in " ".join(first["args"]) and "LMS_AGENT_TOKEN" not in first["env"]
    with pytest.raises(SystemExit) as e:
        cli.main(["ai-eval", "--provider", "gemini"])          # 없음
    assert e.value.code == 2 and "찾지 못했습니다" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["ai-eval", "--provider", "nope"])
    assert e.value.code == 2
    cli.main(["ai-providers"])
    out = capsys.readouterr().out
    assert "[사용 가능 · 안전] codex" in out and "[확인 필요]" not in out.split("codex")[0]
    assert "지금 쓰는 것: codex" in out and "someone" not in out
