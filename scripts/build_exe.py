"""PyInstaller로 실행 파일 하나(dist/lms-agent[.exe])를 만든다.

    python -m pip install pyinstaller
    python scripts/build_exe.py

더블클릭하면 화면이 열린다(인자 없이 실행 = serve).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sep = ";" if sys.platform == "win32" else ":"

cmd = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
    "--name", "lms-agent",
    "--paths", str(SRC),
    # 기능 모듈을 빠뜨리지 않게 하위 모듈을 모두 넣는다(features/__init__.py도 직접 import)
    "--collect-submodules", "lms_agent",
    "--add-data", f"{SRC / 'lms_agent' / 'web' / 'index.html'}{sep}lms_agent/web",
    # 고려대 기본 캐릭터 팩(GIF + NOTICE.md, 고려대학교 자산 — MIT 아님)
    "--add-data", f"{SRC / 'lms_agent' / 'web' / 'characters' / 'ku'}{sep}lms_agent/web/characters/ku",
    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
    str(SRC / "lms_agent" / "__main__.py"),
]
print(" ".join(cmd))
sys.exit(subprocess.call(cmd, cwd=ROOT))
