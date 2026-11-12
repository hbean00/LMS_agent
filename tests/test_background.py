"""백그라운드 동기화(계약 8장): 작업 스케줄러 등록(가짜 실행기), 숨김 실행 스크립트,
앱과 백그라운드가 같은 state.json을 쓸 때 사용자 결정이 사라지지 않는지, CLI 실행 기록, 튜토리얼 설정."""
import json
import os
import subprocess
import urllib.request

import pytest

from lms_agent import cli, core, schedule
from lms_agent.canvas import Canvas
from lms_agent.config import save_config
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store


class FakeRunner:
    def __init__(self):
        self.calls, self.xml, self.registered = [], None, False

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "/Create":
            self.xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
            self.registered = True
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        if args[1] == "/Query":
            if not self.registered:
                return subprocess.CompletedProcess(args, 1, "", "ERROR: not found")
            return subprocess.CompletedProcess(args, 0, self.xml, "")
        if args[1] == "/Delete":
            self.registered = False
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        raise AssertionError(args)


@pytest.fixture()
def fake_windows(monkeypatch):
    monkeypatch.setattr(schedule, "supported", lambda: True)


def test_scheduler_register_query_delete(tmp_path, fake_windows):
    run = FakeRunner()
    s = schedule.Scheduler(tmp_path, runner=run)
    assert s.status() == {"enabled": False, "interval_hours": None}
    s.enable(6)
    assert run.calls[-1][:4] == ["schtasks", "/Create", "/TN", schedule.TASK_NAME]
    assert "<LogonTrigger>" in run.xml and "<Interval>PT6H</Interval>" in run.xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in run.xml       # 꺼져 있던 동안 놓친 실행은 켜지면 바로
    assert "wscript.exe" in run.xml and "background-sync.vbs" in run.xml     # 창 없이
    assert s.status() == {"enabled": True, "interval_hours": 6}
    s.disable()
    assert s.status()["enabled"] is False
    with pytest.raises(ValueError, match="간격"):
        s.enable(5)


def test_launcher_runs_hidden_and_logs(tmp_path):
    vbs = schedule.write_launcher(tmp_path).read_text(encoding="utf-16")
    assert vbs.rstrip().endswith(", 0, False")          # 0 = 창 숨김, False = 기다리지 않음
    assert "sync" in vbs and "--push" in vbs and "--background" in vbs
    assert "LMS_AGENT_HOME" in vbs and "background.log" in vbs
    assert vbs.count('"') % 2 == 0                      # VBScript 따옴표 짝


@pytest.fixture()
def mock(tmp_path):
    srv, base = start_mock_canvas()
    yield srv, base
    srv.shutdown()


def test_user_decision_made_during_background_sync_survives(tmp_path, mock, monkeypatch):
    """백그라운드가 Canvas를 읽는 사이에 사용자가 화면에서 승인해도 그 승인은 남는다."""
    _, base = mock
    path = tmp_path / "state.json"
    app_store = Store(path)
    core.sync(Canvas(base, DEMO_TOKEN), app_store)
    target = next(v for v in app_store.view() if v["status"] == "review")

    bg_store = Store(path)  # 다른 프로세스라고 치자
    real_collect = core.collect

    def collect_and_user_acts(*a, **k):
        cache = real_collect(*a, **k)
        app_store.reload_if_changed()
        app_store.update_user(target["id"], {"decision": "approved"})  # 화면에서 승인
        app_store.save()
        return cache

    monkeypatch.setattr(core, "collect", collect_and_user_acts)
    report = core.sync(Canvas(base, DEMO_TOKEN), bg_store, by="background")
    assert report["by"] == "background"
    final = Store(path)
    assert next(v for v in final.view() if v["id"] == target["id"])["status"] == "scheduled"
    assert final.data["runs"][-1]["by"] == "background"


def test_server_follows_background_writes(tmp_path, mock):
    _, base = mock
    store = Store(tmp_path / "state.json")
    app = App(store, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN}, demo=True)
    core.sync(Canvas(base, DEMO_TOKEN), store)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    try:
        core.sync(Canvas(base, DEMO_TOKEN), Store(tmp_path / "state.json"), by="background")  # 다른 프로세스
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/state", headers={"X-LMS-Agent": app.token})
        st = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert st["runs"][0]["by"] == "background"
        assert st["background"]["last_run"] == st["runs"][0]["finished_at"]
        assert st["background"]["supported"] is False          # 데모는 등록하지 않는다
        assert st["settings"]["tutorial_done"] is False         # 처음엔 튜토리얼을 띄운다
    finally:
        httpd.shutdown()


def test_background_api_with_fake_scheduler(tmp_path, mock, fake_windows):
    _, base = mock
    run = FakeRunner()
    store = Store(tmp_path / "state.json")
    app = App(store, tmp_path, cfg={"canvas_url": base, "token": DEMO_TOKEN},
              scheduler=schedule.Scheduler(tmp_path, runner=run))
    httpd = start_server(app, 0)
    serve_in_thread(httpd)

    def post(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(),
                                     method="POST", headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        code, st = post("/api/background", {"enabled": True, "interval_hours": 3})
        assert code == 200 and st["background"]["enabled"] and st["background"]["interval_hours"] == 3
        assert "sync" in st["background"]["command"]
        code, err = post("/api/background", {"enabled": True, "interval_hours": 7})
        assert code == 400 and "간격" in err["error"]
        code, st = post("/api/background", {"enabled": False})
        assert code == 200 and st["background"]["enabled"] is False
        code, st = post("/api/settings", {"tutorial_done": True})
        assert st["settings"]["tutorial_done"] is True
    finally:
        httpd.shutdown()


def test_cli_background_sync_records_run(tmp_path, mock, monkeypatch, capsys):
    _, base = mock
    monkeypatch.setenv("LMS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("LMS_AGENT_CANVAS_URL", raising=False)
    monkeypatch.delenv("LMS_AGENT_TOKEN", raising=False)
    save_config({"canvas_url": base, "token": DEMO_TOKEN}, tmp_path)
    cli.main(["sync", "--background", "--push"])
    out = capsys.readouterr().out
    assert "확인" in out and "구글: 연결 안 됨" in out
    runs = Store(tmp_path / "state.json").data["runs"]
    assert runs[-1]["by"] == "background"
    cli.main(["sync", "--background"])                     # 두 번째는 바뀐 곳 없음
    assert "확인 0곳" in capsys.readouterr().out
    assert not os.path.exists(tmp_path / "state.lock")     # 잠금 파일이 남지 않는다


def test_review_reminder_event(tmp_path, mock):
    """확인 대기 후보가 있으면 오늘 09:00 '[확인] 마감 후보 N개' 일정(설정을 켰을 때만)."""
    _, base = mock
    store = Store(tmp_path / "state.json")
    core.sync(Canvas(base, DEMO_TOKEN), store)
    _, summaries = core.calendar_payload(store)
    assert not [s for s in summaries if s["uid"].startswith("review-")]       # 기본은 꺼짐
    store.data["settings"]["review_reminder"] = True
    _, summaries = core.calendar_payload(store)
    (rem,) = [s for s in summaries if s["uid"].startswith("review-")]
    n = sum(1 for v in store.view() if v["status"] == "review")
    assert f"마감 후보 {n}개" in rem["summary"] and rem["start"].hour == 9
    for v in store.view():                                                    # 다 처리하면 알림도 사라진다
        if v["status"] == "review":
            store.update_user(v["id"], {"decision": "rejected"})
    _, summaries = core.calendar_payload(store)
    assert not [s for s in summaries if s["uid"].startswith("review-")]
