"""learningx.py를 가짜 Canvas(mock_canvas)의 LearningX 흉내에 붙여 확인한다.

보안 확인은 서버 쪽 기록(server.header_log: 받은 Authorization·Cookie)으로 한다.
"""
from __future__ import annotations

import re
import traceback
import urllib.error
import urllib.request
from datetime import date, timedelta, timezone

import pytest

from lms_agent.canvas import Canvas
from lms_agent.learningx import LearningX, LearningXError, safe_name
from lms_agent.mock_canvas import BOARD_TOOL_ID, DEMO_TOKEN, start_mock_canvas

TODAY = date(2026, 11, 12)
KST = timezone(timedelta(hours=9))
HANGUL = re.compile(r"[가-힣]")


@pytest.fixture
def mock():
    srv, base = start_mock_canvas(today=TODAY, tz=KST)
    yield srv, Canvas(base, DEMO_TOKEN)
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def lx(mock):
    srv, canvas = mock
    x = LearningX(canvas, 101)
    x.launch(LearningX.board_tool(canvas, 101)["id"])
    return srv, canvas, x


def lx_token(srv) -> str:
    (token,) = srv.lx_tokens  # 이 시험에서 발급된 출입증 하나
    return token


def assert_clean(err: LearningXError, srv) -> None:
    """한국어 메시지이고, Canvas 토큰·출입증 값이 메시지·repr·traceback 어디에도 없다."""
    text = "".join(traceback.format_exception(type(err), err, err.__traceback__)) + repr(err)
    assert HANGUL.search(str(err)), str(err)
    for secret in [DEMO_TOKEN, *srv.lx_tokens]:
        assert secret not in text


# ------------------------------------------------------------------ 도구 찾기

def test_board_tool_found_and_none(mock):
    srv, canvas = mock
    tool = LearningX.board_tool(canvas, 101)
    # 'total' 게시판(과목 메뉴에 없음)·출석·계정 수준 도구가 섞여 있어도 과목 게시판을 고른다
    assert (tool["id"], tool["name"]) == (BOARD_TOOL_ID, "게시판")
    assert "/learningx_board/" in tool["url"]
    assert LearningX.board_tool(canvas, 102) is None  # 게시판 없는 과목(무관한 도구만 있음)
    assert LearningX.board_tool(canvas, 999) is None  # 없는 과목(404)
    assert any(p.startswith("/api/v1/courses/101/external_tools?") and "include_parents=true" in p
               for p in srv.requests)


# ------------------------------------------------------------------ 실행 → 게시판 → 글 → 상세

def test_launch_boards_posts_and_detail(lx):
    srv, canvas, x = lx
    assert x.boards() == [{"id": 9101, "title": "과제"}, {"id": 9102, "title": "강의자료실"}]

    # 글 3개, 페이지 크기 1 → 3쪽을 차례로 읽는다(최근 글 먼저)
    first = srv.data["learningx"]["101"]["posts"][9101][0]
    srv.data["learningx"]["101"]["posts"][9101] += [
        {**first, "id": 46010, "title": "Lab7 Assignment", "created_at": "2026-11-11T12:00:00Z", "attachments": []},
        {**first, "id": 46000, "title": "Lab5 Assignment", "created_at": "2026-10-01T12:00:00Z", "attachments": []},
    ]
    srv.lx_page_size = 1
    posts = x.posts(9101)
    assert [p["id"] for p in posts] == [46010, 46001, 46000]
    assert len([p for p in srv.requests if "/boards/9101/posts?page=" in p]) == 3
    lab6 = posts[1]
    assert "due : 23:59 on 11/25" in lab6["content"] and lab6["attachment_count"] == 1
    assert "attachments" not in lab6  # 목록에는 개수만, 첨부는 상세에서
    assert [p["id"] for p in x.posts(9101, max_pages=2)] == [46010, 46001]

    detail = x.post(9101, 46001)
    assert detail["id"] == 46001 and detail["post_url"].endswith("/boards/9101/posts/46001")
    (att,) = detail["attachments"]
    assert (att["filename"], att["canvas_file_id"]) == ("Lab6.pdf", 8001)
    assert "/files/8001/download?" in att["url"] and "verifier=" in att["url"]
    srv.lx_unwrapped = True  # {"item": …}로 감싸지 않은 응답도 같은 결과
    assert x.post(9101, 46001) == detail

    # 보안: 처음부터 끝까지 Canvas 서버 하나에만, Canvas 토큰은 /api/v1에만, LearningX API엔 출입증만
    token = lx_token(srv)
    assert "POST /learningx/lti/learningx_board/boards" in srv.requests
    for entry, auth, cookie in srv.header_log:
        path = entry.split(" ")[-1]
        if path.startswith("/api/v1/"):
            assert auth == f"Bearer {DEMO_TOKEN}"
        else:
            assert DEMO_TOKEN not in (auth or "")
        if path.startswith("/learningx/api/"):
            assert auth == f"Bearer {token}"
    assert token not in repr(x) and x.requests > 0


# ------------------------------------------------------------------ 첨부 받기

def test_download_follows_redirect_without_auth(lx, tmp_path):
    srv, canvas, x = lx
    att = x.post(9101, 46001)["attachments"][0]
    dest = tmp_path / "ASIC" / "과제" / "Lab6.pdf"
    n = x.download(att, dest)
    want = srv.data["files"][8001]
    assert dest.read_bytes() == want and n == len(want)
    assert not list(tmp_path.rglob("*.part"))
    log = {e.split("?")[0]: (a, c) for e, a, c in srv.header_log}
    # 파일 주소와 저장소 둘 다 인증 헤더 없이, 출입증 쿠키도 없이
    assert log["/files/8001/download"] == (None, None)
    assert log["/storage/8001"] == (None, None)
    assert not any(p.startswith("/api/v1/files/") for p in srv.requests)  # 대체 경로는 안 씀


def test_download_redirect_to_other_host_carries_nothing(lx, tmp_path):
    srv, canvas, x = lx
    other, other_base = start_mock_canvas(today=TODAY, tz=KST)  # 다른 포트 = 다른 서버(저장소)
    try:
        srv.storage_base = other_base
        att = x.post(9101, 46001)["attachments"][0]
        assert x.download(att, tmp_path / "Lab6.pdf") == len(srv.data["files"][8001])
        assert (tmp_path / "Lab6.pdf").read_bytes() == srv.data["files"][8001]
        assert [(e, a, c) for e, a, c in other.header_log] == [("/storage/8001", None, None)]
    finally:
        other.shutdown()
        other.server_close()


def test_download_bad_url_falls_back_to_canvas_files_api(lx, tmp_path):
    srv, canvas, x = lx
    att = x.post(9101, 46001)["attachments"][0]
    want = srv.data["files"][8001]

    # 확인값이 틀린 주소 → Canvas가 로그인 화면으로 보냄 → 그 HTML을 PDF로 저장하지 않고 파일 API로
    bad = {**att, "url": att["url"].replace("verifier=v8001", "verifier=wrong")}
    assert x.download(bad, tmp_path / "a.pdf") == len(want)
    assert (tmp_path / "a.pdf").read_bytes() == want
    assert "/login/canvas" in srv.requests
    files_api = [(e, a) for e, a, _ in srv.header_log if e.startswith("/api/v1/files/8001")]
    assert files_api == [("/api/v1/files/8001", f"Bearer {DEMO_TOKEN}")]

    # 다른 서버 주소는 요청하지 않고(이 서버 기록에 없음) 파일 API로
    n = len(srv.requests)
    foreign = {**att, "url": "http://files.example.net/Lab6.pdf"}
    assert x.download(foreign, tmp_path / "b.pdf") == len(want)
    assert srv.requests[n] == "/api/v1/files/8001"

    # 대체할 파일 id가 없으면 오류(로그인 화면은 저장되지 않음)
    with pytest.raises(LearningXError) as e:
        x.download({**bad, "canvas_file_id": None}, tmp_path / "c.pdf")
    assert "웹 페이지" in str(e.value)
    assert not (tmp_path / "c.pdf").exists()
    assert_clean(e.value, srv)


def test_download_size_limit(lx, tmp_path):
    srv, canvas, x = lx
    att = x.post(9101, 46001)["attachments"][0]
    with pytest.raises(LearningXError) as e:
        x.download(att, tmp_path / "big.pdf", max_bytes=100)
    assert "너무 큽니다" in str(e.value)
    assert not list(tmp_path.iterdir())  # 파일도 조각(.part)도 남기지 않는다
    assert not any(p.startswith("/api/v1/files/") for p in srv.requests)  # 크기 초과는 대체 경로로 다시 받지 않는다


# ------------------------------------------------------------------ 실패

@pytest.mark.parametrize("mode, words", [
    ("sessionless", "실행하지 못했습니다"),
    ("form", "양식을 찾지 못했습니다"),
    ("foreign", "다른 서버"),
    ("launch", "거절"),
    ("cookie", "출입증을 주지 않았습니다"),
])
def test_launch_failures(mock, mode, words):
    srv, canvas = mock
    srv.lx_fail = mode
    x = LearningX(canvas, 101)
    with pytest.raises(LearningXError) as e:
        x.launch(BOARD_TOOL_ID)
    assert words in str(e.value)
    assert_clean(e.value, srv)
    if mode == "foreign":
        assert not any(p.startswith("POST ") for p in srv.requests)  # 다른 서버로 양식을 보내지 않았다
    with pytest.raises(LearningXError) as e:
        x.boards()  # 출입증이 없으니 API를 부르지 않는다
    assert "먼저" in str(e.value)
    assert not any(p.startswith("/learningx/api/") for p in srv.requests)


def test_launch_wrong_tool_and_foreign_launch_url(mock, monkeypatch):
    srv, canvas = mock
    with pytest.raises(LearningXError) as e:
        LearningX(canvas, 101).launch(3)  # 게시판이 아닌 도구
    assert "(404)" in str(e.value) and e.value.status == 404
    assert_clean(e.value, srv)

    # Canvas가 다른 서버의 실행 주소를 주면 그곳에 아무것도 보내지 않는다
    monkeypatch.setattr(canvas, "_get_one", lambda path, params=None: {"url": "http://evil.example.net/launch"})
    x = LearningX(canvas, 101)
    with pytest.raises(LearningXError) as e:
        x.launch(BOARD_TOOL_ID)
    assert "다른 서버" in str(e.value) and x.requests == 0


def test_api_401_is_korean_and_token_free(lx):
    srv, canvas, x = lx
    token = lx_token(srv)
    srv.lx_fail = "api"  # 출입증 만료
    with pytest.raises(LearningXError) as e:
        x.boards()
    assert e.value.status == 401 and "출입증" in str(e.value)
    assert_clean(e.value, srv)
    assert token not in repr(x)
    with pytest.raises(LearningXError) as e:  # 거절된 출입증은 버렸다 → 다시 실행해야 함
        x.posts(9101)
    assert "먼저" in str(e.value)
    srv.lx_fail = None
    x.launch(BOARD_TOOL_ID)  # 다시 실행하면 새 출입증으로 된다
    assert [b["id"] for b in x.boards()] == [9101, 9102]


def test_api_rejects_without_bearer(lx):
    srv, canvas, x = lx
    url =f"{srv.base_url}/learningx/api/v1/learningx_board/courses/101/boards"
    for headers in ({}, {"Authorization": f"Bearer {DEMO_TOKEN}"}):  # 출입증 없이, Canvas 토큰으로: 둘 다 401
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)
        assert e.value.code == 401
        e.value.close()


# ------------------------------------------------------------------ 안전한 이름

def test_safe_name_windows_rules():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_name("보고서. . ") == "보고서"
    assert safe_name("줄\n바꿈\t탭\x01끝") == "줄 바꿈 탭_끝"
    assert safe_name("...") == safe_name("") == safe_name(None) == safe_name("  ") == "_"
    assert safe_name("CON") == "_CON" and safe_name("nul.pdf") == "_nul.pdf" and safe_name("com1.txt") == "_com1.txt"
    assert safe_name("CONSOLE") == "CONSOLE" and safe_name("Lab6.pdf") == "Lab6.pdf"


def test_safe_name_long_names_keep_extension():
    long_pdf = safe_name("가" * 200 + ".pdf")
    assert len(long_pdf) == 80 and long_pdf.endswith(".pdf") and long_pdf.startswith("가")
    assert safe_name("a" * 200) == "a" * 80
    cut = safe_name("a" * 78 + ". b" * 10)  # 자른 끝에 점·공백이 남지 않는다
    assert len(cut) <= 80 and not cut.endswith((".", " "))
    assert safe_name("x" * 30 + ".pdf", limit=20) == "x" * 16 + ".pdf"
