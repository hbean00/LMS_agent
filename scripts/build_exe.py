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
    "--add-data", f"{SRC / 'lms_agent' / 'web' / 'index.html'}{sep}lms_agent/web",
    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
    str(SRC / "lms_agent" / "__main__.py"),
]
print(" ".join(cmd))
sys.exit(subprocess.call(cmd, cwd=ROOT))
