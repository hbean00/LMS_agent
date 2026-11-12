"""로그인(토큰 붙여넣기 → 실제 연결 확인)·로그아웃, 그리고 설정 파일에 비밀값이 평문으로 남지 않는지(계약 9장)."""
import json
import sys
import urllib.error
import urllib.request

import pytest

from lms_agent import vault
from lms_agent.config import CONFIG_FILE, load_config, save_config
from lms_agent.mock_canvas import DEMO_TOKEN, start_mock_canvas
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LMS_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("LMS_AGENT_CANVAS_URL", raising=False)
    srv, base = start_mock_canvas()
    app = App(Store(tmp_path / "state.json"), tmp_path, cfg={})
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    yield app, base, tmp_path
    httpd.shutdown()
    srv.shutdown()


def post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_login_checks_canvas_then_logout(env):
    app, base, home = env
    code, err = post(app, "/api/account/login", {"canvas_url": base, "token": "wrong-token"})
    assert code == 400 and "토큰" in err["error"]

    code, st = post(app, "/api/account/login", {"canvas_url": base, "token": DEMO_TOKEN})
    assert code == 200 and st["account"]["logged_in"] and st["account"]["user_name"] == "데모 학생"
    assert st["account"]["token_page"] == f"{base}/profile/settings"
    assert DEMO_TOKEN not in json.dumps(st)                       # 화면 응답에 토큰 없음

    raw = (home / CONFIG_FILE).read_text("utf-8")
    if vault.available():
        assert DEMO_TOKEN not in raw and "token_enc" in raw       # 파일에는 암호화된 값만
    assert load_config(home)["token"] == DEMO_TOKEN               # 읽을 때는 풀린다

    code, st = post(app, "/api/account/logout", {"wipe": True})
    assert code == 200 and not st["account"]["logged_in"] and st["items"] == []
    assert "token" not in load_config(home)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 암호화(DPAPI)")
def test_google_tokens_encrypted_on_disk(tmp_path):
    cfg = {"canvas_url": "https://c.example", "token": "canvas-secret",
           "google": {"calendar_id": "cal1", "tokens": {"access_token": "ya29.secret", "refresh_token": "1//refresh"}}}
    save_config(cfg, tmp_path)
    raw = (tmp_path / CONFIG_FILE).read_text("utf-8")
    for secret in ("canvas-secret", "ya29.secret", "1//refresh"):
        assert secret not in raw
    back = load_config(tmp_path)
    assert back["token"] == "canvas-secret" and back["google"]["tokens"]["refresh_token"] == "1//refresh"
    assert back["google"]["calendar_id"] == "cal1"
    assert cfg["token"] == "canvas-secret"  # 저장해도 앱 안의 값은 그대로(평문 키)


def test_config_from_other_machine_asks_login_again(tmp_path):
    (tmp_path / CONFIG_FILE).write_text(json.dumps({"canvas_url": "https://c.example", "token_enc": "bm90LWRwYXBp"}),
                                        "utf-8")
    cfg = load_config(tmp_path)
    assert "token" not in cfg and cfg["canvas_url"] == "https://c.example"


def test_demo_cannot_login(tmp_path):
    app = App(Store(tmp_path / "s.json"), tmp_path, cfg={}, demo=True)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    try:
        code, err = post(app, "/api/account/login", {"token": "x"})
        assert code == 400 and "데모" in err["error"]
    finally:
        httpd.shutdown()


def test_logout_during_sync_is_not_undone(env, monkeypatch):
    """동기화가 Canvas를 읽는 도중 로그아웃(기록 지우기)하면, 끝난 동기화가 지운 기록을 되살리면 안 된다."""
    import threading

    from lms_agent import core

    app, base, home = env
    reading, release = threading.Event(), threading.Event()
    real_collect = core.collect

    def slow_collect(*a, **kw):
        out = real_collect(*a, **kw)
        reading.set()
        release.wait(10)  # 로그아웃이 끝날 때까지 저장 직전에서 멈춘다
        return out

    monkeypatch.setattr(core, "collect", slow_collect)
    code, _ = post(app, "/api/account/login", {"canvas_url": base, "token": DEMO_TOKEN})
    assert code == 200 and reading.wait(10)
    code, st = post(app, "/api/account/logout", {"wipe": True})
    assert code == 200 and st["items"] == []
    release.set()
    for _ in range(200):  # 백그라운드 동기화가 끝나기를 기다린다
        if not app.syncing:
            break
        threading.Event().wait(0.05)
    assert not app.syncing
    assert app.store.data["items"] == [] and app.store.data["cache"] is None
    assert Store(home / "state.json").data["items"] == []  # 파일에도 되살아나지 않았다


def test_sync_cancelled_writes_nothing(tmp_path):
    from lms_agent import core
    from lms_agent.canvas import Canvas

    srv, base = start_mock_canvas()
    try:
        store = Store(tmp_path / "state.json")
        with pytest.raises(core.SyncCancelled):
            core.sync(Canvas(base, DEMO_TOKEN), store, still_valid=lambda: False)
        assert not (tmp_path / "state.json").exists() and store.data["items"] == []
    finally:
        srv.shutdown()
