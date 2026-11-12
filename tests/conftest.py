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
