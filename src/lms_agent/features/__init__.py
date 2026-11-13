"""기능 모듈 목록. 불러오는 순간 api.router에 경로·상태 칸·이벤트가 등록된다.

새 기능은 이 폴더에 파일 하나를 만들고 load_all()에 더한다(api.py 맨 위 예시 참고).
이름으로 불러오지(import_module) 않고 직접 import하는 이유: PyInstaller exe가 기능 파일을 빠뜨리지 않게.
"""


def load_all() -> None:
    from . import account, ai, background, celebrate, core, google, grades, mascot, materials, update  # noqa: F401
    # 기능마다 경로·상태 칸 이름이 달라 불러오는 순서는 상관없다. 같은 경로를 여럿이 받으면 priority로 정한다.
