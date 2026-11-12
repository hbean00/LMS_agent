"""설치하지 않아도 src/의 lms_agent를 import할 수 있게 경로를 넣는다."""
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
