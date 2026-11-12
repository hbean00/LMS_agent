"""pytest 출력에서 실패한 테스트를 GitHub Actions 주석(::error::)으로 올린다.

주석은 로그인하지 않아도 API로 볼 수 있어서(공개 저장소), 어느 OS에서 무엇이 실패했는지 바로 알 수 있다.
    python scripts/ci_annotate.py pytest-out.txt
"""
import re
import sys
from pathlib import Path

MAX = 10  # GitHub는 단계마다 error 주석을 10개까지만 보여 준다


def _escape(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def _prop(s: str) -> str:
    """title= 같은 속성 값: ':'·','도 바꿔야 'tests/a.py::test_x'가 잘리지 않는다."""
    return _escape(s).replace(":", "%3A").replace(",", "%2C")


def main(path: str) -> int:
    text = Path(path).read_text("utf-8", errors="replace")
    failed = re.findall(r"^(?:FAILED|ERROR) (\S+?)(?: - (.*))?$", text, flags=re.M)
    for name, msg in failed[:MAX - 1]:
        print(f"::error title={_prop(name)[:160]}::{_escape((msg or '').strip())[:500] or '실패'}")
    if len(failed) >= MAX:
        print(f"::error title=그 밖의 실패::{len(failed) - (MAX - 1)}개 더 — 로그를 보세요")
    if not failed:  # 수집 오류 등: 마지막 줄들을 그대로
        tail = [ln for ln in text.splitlines() if ln.strip()][-6:]
        print(f"::error title=pytest::{_escape(' | '.join(tail))[:900]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pytest-out.txt"))
