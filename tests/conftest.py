"""설치하지 않아도 src/의 lms_agent를 import할 수 있게 경로를 넣는다.
그리고 테스트가 실제 문서 폴더·데이터 폴더에 파일을 만들지 않게 막는다."""
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture(autouse=True)
def _isolate_user_folders(tmp_path_factory, monkeypatch):
    """첨부 다운로드(LMS_AGENT_DOWNLOADS)와 데이터 폴더(LMS_AGENT_HOME 기본값)를 임시 폴더로."""
    monkeypatch.setenv("LMS_AGENT_DOWNLOADS", str(tmp_path_factory.mktemp("downloads")))
    monkeypatch.setenv("LMS_AGENT_HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _no_update_network(monkeypatch):
    """업데이트 확인·다운로드가 실제 GitHub으로 나가지 않게 막는다. 테스트는 가짜 fetch를 넘긴다."""
    from lms_agent import updater

    def blocked(req, timeout):
        raise AssertionError("테스트에서 실제 네트워크로 업데이트를 확인하려 했습니다: " + req.full_url)

    monkeypatch.setattr(updater, "_open", blocked)
