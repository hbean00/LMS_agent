"""업데이트(계약 19장): 버전 비교, 설치 방식 찾기, 확인 캐시(하루 한 번·인터넷 없음), 체크섬, exe 교체,
pip·git 적용, 상태 파일 맞추기(migrate), 화면 API. 실제 네트워크·실제 설치·실제 다시 시작은 쓰지 않는다
(가짜 fetch·runner·relaunch, conftest가 진짜 네트워크를 막아 둔다)."""
import base64
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from lms_agent import __version__, migrate, updater
from lms_agent.config import CONFIG_FILE, STATE_FILE
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store
from lms_agent.timeutil import iso, now_local, parse_iso

NEW = "9.0.0"           # 언제나 지금 버전보다 새 버전
TAG = "v" + NEW
DL = f"https://github.com/{updater.REPO}/releases/download/{TAG}/"
EXE_BYTES = b"new exe \x00\x01 bytes"
WHEEL = f"lms_agent-{NEW}-py3-none-any.whl"
WHEEL_BYTES = b"PK fake wheel"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def release(assets=None, tag=TAG, body="- 업데이트 기능\n- 고친 것"):
    names = assets if assets is not None else ["lms-agent-windows.exe", "lms-agent-macos", "lms-agent-linux", WHEEL,
                                               updater.SUMS_NAME]
    return {"tag_name": tag, "html_url": f"https://github.com/{updater.REPO}/releases/tag/{tag}", "body": body,
            "published_at": "2026-11-20T01:00:00Z", "draft": False, "prerelease": False,
            "assets": [{"name": n, "browser_download_url": DL + n, "size": 10} for n in names]}


class FakeNet:
    """URL → 바이트(또는 UpdateError). 부른 주소를 기록한다."""

    def __init__(self, rel=None, files=None, sums=None):
        self.rel = release() if rel is None else rel
        self.files = {"lms-agent-windows.exe": EXE_BYTES, "lms-agent-macos": EXE_BYTES, "lms-agent-linux": EXE_BYTES,
                      WHEEL: WHEEL_BYTES, **(files or {})}
        self.sums = sums if sums is not None else "".join(f"{sha(v)}  {k}\n" for k, v in self.files.items())
        self.calls = []

    def __call__(self, url, max_bytes):
        self.calls.append(url)
        if isinstance(self.rel, Exception):
            if url == updater.API_LATEST:
                raise self.rel
        if url == updater.API_LATEST:
            return json.dumps(self.rel).encode()
        name = url.rsplit("/", 1)[-1]
        if name == updater.SUMS_NAME:
            return self.sums.encode()
        return self.files[name]


def make_updater(tmp_path, net=None, how="exe", **kw):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    store = Store(home / STATE_FILE)
    kw.setdefault("exe_path", tmp_path / "bin" / "lms-agent.exe")
    kw.setdefault("platform", "win32")
    return updater.Updater(store, home, fetch=net or FakeNet(), how=how, **kw)


# ---------------------------------------------------------------- 버전

def test_version_compare():
    assert updater.is_newer("v0.6.0", "0.5.0")
    assert updater.is_newer("0.10.0", "0.9.9")          # 글자가 아니라 숫자로 비교
    assert updater.is_newer("1.0", "0.99.99")
    assert not updater.is_newer("0.5", "0.5.0")          # 같은 버전
    assert not updater.is_newer("v0.5.0", "0.6.0")
    assert updater.is_newer("0.6.0", "0.6.0-rc1") and not updater.is_newer("0.6.0-rc1", "0.6.0")  # 미리 보기 < 정식
    assert not updater.is_newer("latest", "0.5.0") and not updater.is_newer(None, "0.5.0")
    assert updater.clean_version("v0.6.0") == "0.6.0" and updater.clean_version("../x") is None


# ---------------------------------------------------------------- 설치 방식

def test_install_type(tmp_path):
    assert updater.install_type(frozen=True) == "exe"
    site = tmp_path / "venv" / "Lib" / "site-packages" / "lms_agent"
    assert updater.install_type(frozen=False, pkg=site) == "pip"
    assert updater.install_type(frozen=False, pkg=tmp_path / "usr" / "lib" / "dist-packages" / "lms_agent") == "pip"
    repo = tmp_path / "repo"
    (repo / "src" / "lms_agent").mkdir(parents=True)
    assert updater.install_type(frozen=False, pkg=repo / "src" / "lms_agent") == "unknown"   # .git 없음
    (repo / ".git").mkdir()
    assert updater.install_type(frozen=False, pkg=repo / "src" / "lms_agent") == "git"
    assert updater.git_root(repo / "src" / "lms_agent") == repo
    wt = tmp_path / "worktree"
    (wt / "src" / "lms_agent").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt")      # git worktree는 .git이 파일
    assert updater.install_type(frozen=False, pkg=wt / "src" / "lms_agent") == "git"


# ---------------------------------------------------------------- 주소·리다이렉트·오류

def test_only_https_github_urls():
    updater.check_url("https://api.github.com/repos/x/y/releases/latest")
    updater.check_url("https://objects.githubusercontent.com/github-production-release-asset/1")
    for bad in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "file:///etc/passwd", ""):
        with pytest.raises(updater.UpdateError):
            updater.check_url(bad)
    h = updater._GitHubOnlyRedirects()
    req = urllib.request.Request("https://github.com/a")
    with pytest.raises(updater.UpdateError):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/payload")
    assert h.redirect_request(req, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/x") is not None


def _http_error(code, headers=None):
    from email.message import Message
    m = Message()
    for k, v in (headers or {}).items():
        m[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", m, io.BytesIO(b""))


@pytest.mark.parametrize("exc, kind", [
    (_http_error(404), "no_release"),
    (_http_error(403, {"X-RateLimit-Remaining": "0"}), "rate_limited"),
    (_http_error(429), "rate_limited"),
    (_http_error(500), "http"),
    (urllib.error.URLError("no network"), "offline"),
    (TimeoutError(), "offline"),
])
def test_http_fetch_errors(monkeypatch, exc, kind):
    def boom(req, timeout):
        assert req.get_header("User-agent") == updater.USER_AGENT and not req.get_header("Authorization")
        raise exc
    monkeypatch.setattr(updater, "_open", boom)
    with pytest.raises(updater.UpdateError) as e:
        updater.http_fetch(updater.API_LATEST, 100)
    assert e.value.kind == kind


def test_http_fetch_limits_size(monkeypatch):
    class Resp(io.BytesIO):
        def geturl(self):
            return updater.API_LATEST
    monkeypatch.setattr(updater, "_open", lambda req, timeout: Resp(b"x" * 50))
    assert updater.http_fetch(updater.API_LATEST, 50) == b"x" * 50
    with pytest.raises(updater.UpdateError, match="너무 커서"):
        updater.http_fetch(updater.API_LATEST, 49)


# ---------------------------------------------------------------- 확인과 캐시

def test_check_caches_once_a_day(tmp_path):
    net = FakeNet()
    u = make_updater(tmp_path, net)
    assert u.due() and not u.available()
    c = u.check()
    assert c["latest"] == NEW and c["tag"] == TAG and c["error"] is None and "업데이트 기능" in c["notes"]
    assert {a["name"] for a in c["assets"]} >= {"lms-agent-windows.exe", WHEEL, updater.SUMS_NAME}
    assert u.available() and not u.due()
    saved = json.loads((u.home / STATE_FILE).read_text("utf-8"))["update"]   # state.json에 남는다
    assert saved["latest"] == NEW and parse_iso(saved["checked_at"]) is not None
    later = parse_iso(saved["checked_at"]) + timedelta(hours=25)
    u.now = lambda: later
    assert u.due()
    st = u.status()
    assert st["available"] and st["current"] == __version__ and st["latest"] == NEW and st["how"] == "exe"
    assert st["can_apply"] and st["notes_url"].endswith("/tag/" + TAG)


def test_check_offline_keeps_last_answer_and_retries_sooner(tmp_path):
    net = FakeNet()
    u = make_updater(tmp_path, net)
    u.check()
    net.rel = updater.UpdateError("인터넷에 연결되지 않아 새 버전을 확인하지 못했어요", "offline")
    t0 = now_local() + timedelta(days=2)
    u.now = lambda: t0
    c = u.check()
    assert c["latest"] == NEW and "인터넷" in c["error"]      # 지난번 답은 그대로, 오류는 조용히 칸에만
    assert not u.due()
    u.now = lambda: t0 + timedelta(hours=4)                   # 실패하면 하루가 아니라 몇 시간 뒤 다시
    assert u.due()


def test_check_without_release_or_bad_json(tmp_path):
    net = FakeNet(rel=updater.UpdateError("아직 공개된 버전이 없어요", "no_release"))
    u = make_updater(tmp_path, net)
    c = u.check()
    assert c["latest"] is None and c["error"] is None and not u.available()

    class Garbage(FakeNet):
        def __call__(self, url, max_bytes):
            return b"<html>not json"
    u2 = make_updater(tmp_path / "b", Garbage())
    assert "읽지 못했" in u2.check()["error"]


def test_check_can_be_turned_off(tmp_path):
    u = make_updater(tmp_path)
    u.store.data["settings"]["update_check"] = False
    assert not u.due()


def test_dismiss_until_newer_version(tmp_path):
    net = FakeNet()
    u = make_updater(tmp_path, net)
    u.check()
    u.dismiss()
    assert u.status()["dismissed"]
    net.rel = release(tag="v9.1.0")
    u.check()
    assert not u.status()["dismissed"]                        # 더 새 버전이 나오면 다시 알린다


# ---------------------------------------------------------------- 체크섬

def test_checksums():
    good = sha(b"abc")
    sums = updater.parse_sums(f"{good.upper()}  lms-agent-linux\n{sha(b'x')} *lms_agent-1.0-py3-none-any.whl\nnot a line\n")
    assert sums == {"lms-agent-linux": good, "lms_agent-1.0-py3-none-any.whl": sha(b"x")}
    assert updater.checksum_ok(sha(b"abc"), good)
    assert not updater.checksum_ok(sha(b"abd"), good)
    assert not updater.checksum_ok(good, None) and not updater.checksum_ok(good, "1234")


# ---------------------------------------------------------------- exe

def _fake_exe(tmp_path) -> Path:
    exe = tmp_path / "bin" / "lms-agent.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"old exe")
    return exe


def _seed_data(u):
    (u.home / CONFIG_FILE).write_text('{"canvas_url": "https://c.example"}', "utf-8")
    u.store.data["user"] = {"x": {"decision": "approved"}}
    u.store.save()


def test_exe_update_verifies_backs_up_and_swaps(tmp_path):
    exe = _fake_exe(tmp_path)
    u = make_updater(tmp_path, exe_path=exe)
    _seed_data(u)
    u.check()
    r = u.prepare()
    assert r["ok"] and r["restart"] and callable(r["finish"])
    assert exe.read_bytes() == b"old exe"                     # 교체는 finish() 때(화면이 '다시 켜는 중'을 본 뒤)
    assert (exe.parent / "lms-agent.exe.new").read_bytes() == EXE_BYTES
    backup = u.home / f"state.before-{NEW}.json"
    assert json.loads(backup.read_text("utf-8"))["user"] == {"x": {"decision": "approved"}}
    assert (u.home / f"config.before-{NEW}.json").read_text("utf-8") == '{"canvas_url": "https://c.example"}'
    assert u.cached()["applied"]["version"] == NEW and u.cached()["applied"]["from"] == __version__
    r["finish"]()
    assert exe.read_bytes() == EXE_BYTES and (exe.parent / "lms-agent.exe.old").read_bytes() == b"old exe"
    assert not (exe.parent / "lms-agent.exe.new").exists()
    # 다음에 켤 때 옛 파일을 지운다(같은 경로라 작업 스케줄러의 백그라운드 동기화도 새 파일을 쓴다)
    u.startup_cleanup(background=False)
    assert sorted(p.name for p in exe.parent.iterdir()) == ["lms-agent.exe"]


def test_exe_bad_checksum_changes_nothing(tmp_path):
    exe = _fake_exe(tmp_path)
    net = FakeNet(files={"lms-agent-windows.exe": b"tampered"}, sums=f"{sha(EXE_BYTES)}  lms-agent-windows.exe\n")
    u = make_updater(tmp_path, net, exe_path=exe)
    _seed_data(u)
    u.check()
    r = u.prepare()
    assert not r["ok"] and "확인값" in r["message"]
    assert exe.read_bytes() == b"old exe"
    assert sorted(p.name for p in exe.parent.iterdir()) == ["lms-agent.exe"]       # 받은 것도 남기지 않음
    assert not list(u.home.glob("*.before-*"))                                     # 백업 전에 멈춤
    assert "applied" not in u.cached()


def test_exe_needs_checksum_file_and_matching_asset(tmp_path):
    u = make_updater(tmp_path, FakeNet(rel=release(assets=["lms-agent-windows.exe"])))
    u.check()
    st = u.status()
    assert st["available"] and not st["can_apply"] and "SHA256SUMS" in st["reason"] and st["command"] is None
    u2 = make_updater(tmp_path / "mac", FakeNet(rel=release(assets=["lms-agent-windows.exe", updater.SUMS_NAME])),
                      platform="darwin")
    u2.check()
    assert not u2.status()["can_apply"] and "lms-agent-macos" in u2.status()["reason"]
    u3 = make_updater(tmp_path / "c", FakeNet(sums=f"{sha(b'x')}  something-else\n"))
    u3.check()
    r = u3.prepare()
    assert not r["ok"] and "lms-agent-windows.exe" in r["message"]


def test_swap_rolls_back_when_second_move_fails(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    new = exe.with_name("lms-agent.exe.new")
    new.write_bytes(b"new")
    real = os.replace
    calls = []

    def flaky(a, b):
        if Path(b).parent != exe.parent:      # 다른 스레드의 저장 등은 그대로
            return real(a, b)
        calls.append((Path(a).name, Path(b).name))
        if len(calls) == 2:
            raise PermissionError("locked")
        return real(a, b)
    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(PermissionError):
        updater.swap_executable(exe, new)
    assert exe.read_bytes() == b"old exe"                     # 원래대로
    assert calls[-1] == ("lms-agent.exe.old", "lms-agent.exe")


def test_restart_command_and_environment(tmp_path):
    exe = tmp_path / "lms-agent.exe"
    assert updater.restart_argv("exe", 8765, exe) == [str(exe), "serve", "--no-browser", "--port", "8765"]
    py = updater.restart_argv("pip", 8766)
    assert py[1:] == ["-m", "lms_agent", "serve", "--no-browser", "--port", "8766"]
    env = updater.clean_env({"PATH": "x", "_MEIPASS2": "C:/tmp/_MEI1", "_PYI_APPLICATION_HOME_DIR": "y", "LMS_AGENT_HOME": "h"})
    assert env == {"PATH": "x", "LMS_AGENT_HOME": "h", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    script = base64.b64decode(updater.windows_finish_script(
        Path("C:/Users/김현빈/Down's/lms-agent.exe.new"), Path("C:/Users/김현빈/Down's/lms-agent.exe"),
        ["x.exe", "serve", "--port", "8765"])).decode("utf-16-le")
    assert "Down''s" in script and "김현빈" in script and "'serve', '--port', '8765'" in script
    assert script.count("{") == script.count("}")


def test_auto_update_at_start_only_when_enabled_and_not_twice(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    launched = []
    monkeypatch.setattr(updater, "relaunch", lambda argv: launched.append(argv))
    u = make_updater(tmp_path, exe_path=exe)
    assert not u.auto_update_at_start(out=lambda m: None, argv_tail=[])     # 기본은 꺼짐: 알리기만
    assert exe.read_bytes() == b"old exe" and not launched
    u.store.data["settings"]["auto_update"] = True
    assert u.auto_update_at_start(out=lambda m: None, argv_tail=["serve"])
    assert exe.read_bytes() == EXE_BYTES and launched == [[str(exe), "serve"]]
    # 바꿨는데도(예: 릴리스의 파일이 옛 버전) 다시 켠 앱이 아직 옛 버전이면 되풀이하지 않는다
    exe.write_bytes(b"old exe")
    assert u.stuck() and not u.auto_update_at_start(out=lambda m: None, argv_tail=["serve"])
    assert len(launched) == 1 and "적용되지 않았" in u.status()["error"]


def test_auto_update_never_for_git(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "relaunch", lambda argv: pytest.fail("git은 자동으로 바꾸지 않는다"))
    u = make_updater(tmp_path, how="git", root=tmp_path)
    u.store.data["settings"]["auto_update"] = True
    assert not u.auto_update_at_start(out=lambda m: None)


# ---------------------------------------------------------------- pip·git

class FakeRunner:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
        self.seen_files = {}

    def __call__(self, args, timeout=None, cwd=None, env=None):
        self.calls.append(list(args))
        for a in args:
            if str(a).endswith(".whl") and os.path.exists(a):
                self.seen_files[a] = Path(a).read_bytes()
        code, out = self.results.pop(0) if self.results else (0, "")
        return subprocess.CompletedProcess(args, code, out, "error: boom" if code else "")


def test_pip_update_installs_verified_wheel(tmp_path):
    run = FakeRunner()
    u = make_updater(tmp_path, how="pip", runner=run, python="/py/python")
    _seed_data(u)
    u.check()
    r = u.prepare()
    assert r["ok"] and r["restart"] and "finish" not in r
    (args,) = run.calls
    assert args[:4] == ["/py/python", "-m", "pip", "install"] and "--no-deps" in args and args[-1].endswith(WHEEL)
    assert run.seen_files[args[-1]] == WHEEL_BYTES and not os.path.exists(args[-1])   # 확인한 파일을 설치, 끝나면 지움
    assert (u.home / f"state.before-{NEW}.json").exists()
    st = u.status()
    assert st["command"] == f"python -m pip install -U git+{updater.GIT_URL}@{TAG}"


def test_pip_failure_reports_and_bad_wheel_never_installs(tmp_path):
    run = FakeRunner([(1, "")])
    u = make_updater(tmp_path, how="pip", runner=run)
    u.check()
    r = u.prepare()
    assert not r["ok"] and "pip 설치가 실패" in r["message"] and "boom" in r["message"]
    run2 = FakeRunner()
    u2 = make_updater(tmp_path / "b", FakeNet(files={WHEEL: b"evil"}, sums=f"{sha(WHEEL_BYTES)}  {WHEEL}\n"), how="pip", runner=run2)
    u2.check()
    assert not u2.prepare()["ok"] and run2.calls == []         # 체크섬이 다르면 pip을 부르지도 않는다


def test_git_update_only_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda name: "git")
    run = FakeRunner([(0, " M src/lms_agent/core.py\n")])
    u = make_updater(tmp_path, how="git", root=tmp_path / "repo", runner=run)
    u.check()
    st = u.status()
    assert st["can_apply"] and "pull --ff-only" in st["command"]
    r = u.prepare()
    assert not r["ok"] and "고친 파일" in r["message"] and "pull --ff-only" in r["message"]
    assert [c[3] for c in run.calls] == ["status"]             # 더러우면 pull 하지 않는다
    run.results = [(0, ""), (0, "Fast-forward")]
    r = u.prepare()
    assert r["ok"] and [c[3] for c in run.calls] == ["status", "status", "pull"]


def test_unknown_install_only_shows_how(tmp_path):
    u = make_updater(tmp_path, how="unknown")
    u.check()
    st = u.status()
    assert st["available"] and not st["can_apply"] and st["command"].startswith("python -m pip install -U git+")


# ---------------------------------------------------------------- 상태 파일 맞추기

def test_migrate_old_state_keeps_unknown_fields_and_backs_up(tmp_path):
    p = tmp_path / STATE_FILE
    old = {"version": 1, "items": [], "user": {"a": {"decision": "approved"}}, "settings": {"include_canvas": False},
           "some_future_or_old_field": {"keep": 1}}
    p.write_text(json.dumps(old), "utf-8")
    st = Store(p)
    assert st.data["app_version"] == __version__ and st.data["some_future_or_old_field"] == {"keep": 1}
    assert st.data["user"] == {"a": {"decision": "approved"}} and st.data["settings"]["include_canvas"] is False
    assert isinstance(st.data["update"], dict)
    backup = tmp_path / f"state.before-{__version__}.json"
    assert json.loads(backup.read_text("utf-8")) == old        # 처음 읽기 전 모습 그대로
    st.save()
    st2 = Store(p)
    assert st2.data["some_future_or_old_field"] == {"keep": 1}


def test_state_from_newer_version_is_left_alone(tmp_path):
    p = tmp_path / STATE_FILE
    newer = {"app_version": "99.0.0", "items": [], "user": {}, "settings": {}, "new_thing": [1, 2],
             "update": {"latest": "99.0.0", "future_key": True}}
    p.write_text(json.dumps(newer), "utf-8")
    st = Store(p)
    assert st.data["app_version"] == "99.0.0" and st.data["new_thing"] == [1, 2]
    assert not list(tmp_path.glob("*.before-*"))
    st.save()
    saved = json.loads(p.read_text("utf-8"))
    assert saved["app_version"] == "99.0.0" and saved["update"]["future_key"] is True   # 모르는 칸도 지닌 채 저장
    u = updater.Updater(st, tmp_path, fetch=FakeNet(), how="exe")
    assert u.status()["data_newer"] == "99.0.0"


def test_broken_shapes_open_instead_of_crashing(tmp_path):
    p = tmp_path / STATE_FILE
    p.write_text(json.dumps({"settings": ["oops"], "user": "x", "items": {}}), "utf-8")
    st = Store(p)
    assert st.data["settings"]["include_canvas"] is True and st.data["user"] == {} and st.data["items"] == []
    assert st.data["_repaired"] == {"settings": ["oops"], "user": "x", "items": {}}
    st.view()


def test_migrations_run_in_order_only_for_older_data(monkeypatch):
    ran = []
    monkeypatch.setattr(migrate, "MIGRATIONS", [("0.2.0", lambda d: ran.append("0.2")), ("0.10.0", lambda d: ran.append("0.10"))])
    assert migrate.migrate({"app_version": "0.1.0"}, current="0.10.0") == ["0.2.0", "0.10.0"] and ran == ["0.2", "0.10"]
    ran.clear()
    assert migrate.migrate({"app_version": "0.5.0"}, current="0.10.0") == ["0.10.0"]
    ran.clear()
    d = {"app_version": "0.11.0"}
    assert migrate.migrate(d, current="0.10.0") == [] and ran == [] and d["app_version"] == "0.11.0"


# ---------------------------------------------------------------- 화면 API

def _post(app, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_state(app):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/state", headers={"X-LMS-Agent": app.token})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


@pytest.fixture()
def served(tmp_path):
    started = []

    def make(**opts):
        launched = []
        opts.setdefault("update_fetch", FakeNet())
        opts.setdefault("relaunch", lambda argv: launched.append(argv))
        opts.setdefault("restart_delay", 0)
        app = App(Store(tmp_path / STATE_FILE), tmp_path, cfg={}, **opts)
        httpd = start_server(app, 0)
        serve_in_thread(httpd)
        started.append(httpd)
        return app, launched

    yield make
    for h in started:
        try:
            h.shutdown()
        except Exception:
            pass


def _wait(pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_state_and_check_route(served):
    app, _ = served(update_how="exe", update_platform="win32")
    st = _get_state(app)
    assert st["update"]["current"] == __version__ and st["update"]["latest"] is None and not st["update"]["available"]
    assert st["settings"]["update_check"] is True and st["settings"]["auto_update"] is False
    code, st = _post(app, "/api/update/check")
    assert code == 200 and st["update"]["available"] and st["update"]["latest"] == NEW and st["update"]["phase"] == "idle"
    code, st = _post(app, "/api/settings", {"auto_update": True, "update_check": False})
    assert st["settings"]["auto_update"] is True and st["settings"]["update_check"] is False
    code, st = _post(app, "/api/update/dismiss")
    assert st["update"]["dismissed"]


def test_apply_exe_through_api_restarts_on_same_port(served, tmp_path):
    exe = _fake_exe(tmp_path)
    app, launched = served(update_how="exe", update_exe=exe, update_platform="win32")
    port = app.port
    _post(app, "/api/update/check")
    code, st = _post(app, "/api/update/apply")
    assert code == 200 and st["update"]["phase"] in ("applying", "restarting")
    assert _wait(lambda: launched)
    assert launched == [[str(exe), "serve", "--no-browser", "--port", str(port)]]
    assert exe.read_bytes() == EXE_BYTES and (tmp_path / f"state.before-{NEW}.json").exists()


def test_apply_pip_then_restart(served, monkeypatch):
    run = FakeRunner()
    app, launched = served(update_how="pip", update_runner=run)
    _post(app, "/api/update/check")
    code, st = _post(app, "/api/update/restart")
    assert code == 400                                          # 설치 전에는 다시 시작할 것이 없다
    code, st = _post(app, "/api/update/apply")
    assert code == 200
    assert _wait(lambda: app.updater.phase == "idle" and app.updater.result)
    st = _get_state(app)
    assert st["update"]["result"]["ok"] and st["update"]["result"]["restart"] and not launched
    code, st = _post(app, "/api/update/restart")
    assert code == 200 and st["update"]["phase"] == "restarting"
    assert _wait(lambda: launched)
    assert launched[0][1:4] == ["-m", "lms_agent", "serve"]


def test_apply_refused_in_demo_and_when_current(served, tmp_path):
    app, _ = served(update_how="exe", update_platform="win32", update_fetch=FakeNet(rel=release(tag="v" + __version__)))
    _post(app, "/api/update/check")
    code, err = _post(app, "/api/update/apply")
    assert code == 400 and "최신" in err["error"]
    demo = App(Store(tmp_path / "d" / STATE_FILE), tmp_path / "d", cfg={}, demo=True, update_how="exe",
               update_platform="win32", update_fetch=FakeNet(), relaunch=lambda argv: pytest.fail("demo"))
    httpd = start_server(demo, 0)
    serve_in_thread(httpd)
    try:
        _post(demo, "/api/update/check")
        st = _get_state(demo)
        assert st["update"]["available"] and not st["update"]["can_apply"] and "데모" in st["update"]["reason"]
        code, err = _post(demo, "/api/update/apply")
        assert code == 400 and "데모" in err["error"]
    finally:
        httpd.shutdown()


def test_update_routes_need_session_value(served):
    app, _ = served(update_how="exe")
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/update/apply", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403


def test_just_updated_note_until_dismissed(tmp_path):
    u = make_updater(tmp_path)
    u._save({"applied": {"version": __version__, "from": "0.0.1", "at": iso(now_local())}})
    assert u.status()["updated"]["to"] == __version__
    u.dismiss()
    assert u.status()["updated"] is None


def test_http_fetch_cert_error_explains_like_canvas(monkeypatch):
    """백신·네트워크가 HTTPS를 가로채면 Canvas와 같은 고치는 방법(계약 17장)을 보여 준다(발급자 조회도 가짜로)."""
    import ssl

    from lms_agent import net

    def boom(req, timeout):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate in certificate chain"))
    monkeypatch.setattr(updater, "_open", boom)
    monkeypatch.setattr(net, "peer_issuer", lambda host, port=443, timeout=4.0: "Example AV Root")
    with pytest.raises(updater.UpdateError) as e:
        updater.http_fetch(updater.API_LATEST, 100)
    assert e.value.kind == "offline"
    assert "GitHub(업데이트 확인)" in str(e.value) and "Example AV Root" in str(e.value) and "certs" in str(e.value)
