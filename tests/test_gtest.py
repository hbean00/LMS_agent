"""google-test 명령의 연결 코드(OAuth 리디렉트 받기 → 캘린더 → 반영 두 번)를 가짜 구글로 끝까지 돌린다."""
import urllib.request

from lms_agent import gtest
from lms_agent.mock_google import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, start_mock_google, urls


def test_google_test_command_against_mock(tmp_path):
    srv, base = start_mock_google()
    lines = []
    try:
        code = gtest.run(out=lines.append, urls=urls(base), client=(DEMO_CLIENT_ID, DEMO_CLIENT_SECRET),
                         home=tmp_path, open_browser=lambda u: urllib.request.urlopen(u, timeout=10).read())
    finally:
        srv.shutdown()
    text = "\n".join(lines)
    assert code == 0, text
    assert "2차 반영(같은 내용): 새로 0 · 수정 0 · 삭제 0" in text
    assert DEMO_CLIENT_SECRET not in text


def test_api_key_is_rejected_with_explanation(tmp_path):
    lines = []
    assert gtest.run(out=lines.append, client=("AIzaSyFAKE", None), home=tmp_path) == 2
    assert "OAuth 클라이언트 ID" in lines[0]
