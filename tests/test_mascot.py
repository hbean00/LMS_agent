"""캐릭터: 고려대 Canvas·데모면 앱에 든 KU 캐릭터, 내 캐릭터 폴더가 있으면 그것, 아니면 오리지널 페이스메이커.
내 폴더의 그림만 내보내기(경로 넘기·다른 확장자·두 단계 하위 폴더 막기), 장면 고르기 검사, 폴더 열기."""
import json
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from lms_agent.features import mascot as feature
from lms_agent.server import App, serve_in_thread, start_server
from lms_agent.store import Store

GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
HAS_KU = bool(feature.ku_moods())


def make(tmp_path, canvas_url="https://canvas.example.edu", demo=False):
    chars = tmp_path / "characters"
    opened = []
    app = App(Store(tmp_path / "state.json"), tmp_path, cfg={"canvas_url": canvas_url}, demo=demo,
              mascot_dir=chars, opener=opened.append)
    httpd = start_server(app, 0)
    serve_in_thread(httpd)
    return app, chars, opened, httpd


@pytest.fixture()
def env(tmp_path):
    app, chars, opened, httpd = make(tmp_path)
    yield app, chars, opened
    httpd.shutdown()


@pytest.fixture()
def ku(tmp_path):
    app, chars, opened, httpd = make(tmp_path, "https://mylms.korea.ac.kr")
    yield app, chars, opened
    httpd.shutdown()


def get(app, path, token=False):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", headers={"X-LMS-Agent": app.token} if token else {})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(app, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-LMS-Agent": app.token, "Content-Type": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def state(app):
    return json.loads(get(app, "/api/state", token=True)[2])


def test_other_school_without_pack_uses_original(env):
    app, chars, _ = env
    m = state(app)["mascot"]
    assert m["pack"] is None and m["files"] == [] and m["credit"] is None and m["ku"] is False
    assert set(m["moods"]) == set(feature.MOODS) and all(v is None for v in m["moods"].values())
    assert m["folder"] == str(chars)
    assert get(app, "/mascot/ku/run.gif")[0] == 200 if HAS_KU else True   # 파일은 있어도 기본으로 쓰지 않을 뿐


@pytest.mark.skipif(not HAS_KU, reason="KU 캐릭터 파일이 패키지에 없음")
def test_korea_university_gets_ku_pack(ku):
    app, _, _ = ku
    m = state(app)["mascot"]
    assert m["pack"] == "ku" and m["ku"] and m["credit"] == "캐릭터: 고려대학교 KU BUDDY 호이·쿠이"
    assert m["moods"]["run"] == "/mascot/ku/run.gif" and m["moods"]["grade_a"] == "/mascot/ku/grade_a.gif"
    code, headers, body = get(app, m["moods"]["finish"])
    assert code == 200 and headers["Content-Type"] == "image/gif" and body[:6] in (b"GIF89a", b"GIF87a")
    assert get(app, "/mascot/ku/nope.gif")[0] == 404 and get(app, "/mascot/ku/..%2Fx.gif")[0] == 404
    # 원하면 오리지널로
    code, st = post(app, "/api/mascot/map", {"choice": "original"})
    assert code == 200 and st["mascot"]["pack"] is None and st["mascot"]["moods"]["run"] is None
    assert post(app, "/api/mascot/map", {"choice": "ku-only"})[0] == 400


@pytest.mark.skipif(not HAS_KU, reason="KU 캐릭터 파일이 패키지에 없음")
def test_demo_uses_ku_pack(tmp_path):
    app = App(Store(tmp_path / "s.json"), tmp_path, cfg={"canvas_url": "http://127.0.0.1:9"}, demo=True)
    m = app.state()["mascot"]
    assert m["pack"] == "ku" and m["folder"] == str(tmp_path / "characters")


def fill(chars):
    chars.mkdir()
    (chars / "run.gif").write_bytes(GIF)
    (chars / "축하.png").write_bytes(PNG)
    (chars / "notes.txt").write_text("not an image", "utf-8")
    (chars / "evil.html").write_text("<script>alert(1)</script>", "utf-8")
    (chars / "호이").mkdir()
    (chars / "호이" / "rest.gif").write_bytes(GIF)
    (chars / "호이" / "deeper").mkdir()
    (chars / "호이" / "deeper" / "too_deep.png").write_bytes(PNG)
    (chars.parent / "outside.png").write_bytes(PNG)


def test_local_pack_state_and_serving(env):
    app, chars, _ = env
    fill(chars)
    m = state(app)["mascot"]
    assert m["pack"] == "local"
    assert m["files"] == ["run.gif", "축하.png", "호이/rest.gif"]        # 그림만, 하위 폴더는 한 단계까지
    assert m["moods"]["run"] == "/mascot/local/run.gif"                 # 이름이 장면과 같음
    assert m["moods"]["rest"] == "/mascot/local/" + quote("호이/rest.gif")
    assert m["moods"]["grade_a"] == "/mascot/local/" + quote("축하.png")  # 낱말로 짐작
    assert m["moods"]["finish"] is None
    code, headers, body = get(app, m["moods"]["rest"])
    assert code == 200 and body == GIF and headers["Content-Type"] == "image/gif"   # 그대로(고치지 않음)
    assert "sandbox" in headers["Content-Security-Policy"]


@pytest.mark.skipif(not HAS_KU, reason="KU 캐릭터 파일이 패키지에 없음")
def test_local_pack_overrides_ku_per_mood(ku):
    app, chars, _ = ku
    fill(chars)
    m = state(app)["mascot"]
    assert m["pack"] == "local" and m["moods"]["run"] == "/mascot/local/run.gif"
    assert m["moods"]["finish"] == "/mascot/ku/finish.gif" and m["credit"]       # 고르지 않은 장면은 KU


@pytest.mark.parametrize("path", [
    "/mascot/local/notes.txt", "/mascot/local/evil.html", "/mascot/local/..%2Foutside.png",
    "/mascot/local/..%5Coutside.png", "/mascot/local/../outside.png", "/mascot/local/%2e%2e%2fstate.json",
    "/mascot/local/C%3A%5CWindows%5Cwin.ini", "/mascot/local/missing.png", "/mascot/local/..",
    "/mascot/local/pack.json", "/mascot/local/" + quote("호이/deeper/too_deep.png"),
    "/mascot/local/" + quote("호이\\rest.gif"), "/mascot/run.gif",
])
def test_serving_only_listed_images(env, path):
    app, chars, _ = env
    fill(chars)
    (chars / "pack.json").write_text("{}", "utf-8")
    code, _, _ = get(app, path)
    assert code == 404


def test_map_mood_validation_and_pack_json(env):
    app, chars, _ = env
    fill(chars)
    code, st = post(app, "/api/mascot/map", {"mood": "finish", "file": "호이/rest.gif"})
    assert code == 200 and st["mascot"]["moods"]["finish"] == "/mascot/local/" + quote("호이/rest.gif")
    assert st["mascot"]["moods"]["run"] == "/mascot/local/run.gif"          # 짐작해 둔 것도 옮겨 적음
    pack = json.loads((chars / "pack.json").read_text("utf-8"))
    assert pack["finish"] == "호이/rest.gif" and pack["run"] == "run.gif"
    code, st = post(app, "/api/mascot/map", {"mood": "run", "file": None})  # 기본 캐릭터로
    assert code == 200 and st["mascot"]["moods"]["run"] is None
    code, st = post(app, "/api/mascot/map", {"name": "호이"})
    assert code == 200 and st["mascot"]["name"] == "호이"
    for body in ({"mood": "dance", "file": "run.gif"}, {"mood": "run", "file": "../outside.png"},
                 {"mood": "run", "file": "notes.txt"}, {"mood": "run", "file": "호이/deeper/too_deep.png"},
                 {"mood": "run", "file": 3}, {}):
        code, err = post(app, "/api/mascot/map", body)
        assert code == 400 and err["error"]


def test_broken_pack_json_and_missing_files_ignored(env):
    app, chars, _ = env
    fill(chars)
    (chars / "pack.json").write_text('{"grade_a": "gone.gif", "run": 5', "utf-8")   # 깨진 파일
    m = state(app)["mascot"]
    assert m["moods"]["grade_a"] == "/mascot/local/" + quote("축하.png")          # 짐작으로 돌아감
    (chars / "pack.json").write_text(json.dumps({"grade_a": "gone.gif"}), "utf-8")
    m = state(app)["mascot"]
    assert m["moods"]["grade_a"] is None and m["moods"]["run"] is None             # 직접 고른 뒤에는 짐작하지 않음


def test_open_folder(env):
    app, chars, opened = env
    code, body = post(app, "/api/mascot/open-folder", {})
    assert code == 200 and body == {"ok": True} and opened == [chars] and chars.is_dir()
