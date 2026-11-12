"""비밀값 암호화: Windows DPAPI(CryptProtectData)로 Canvas 토큰 같은 값을 암호화해 저장한다.

- 이 PC의 이 Windows 사용자만 풀 수 있다(다른 PC로 설정 파일을 옮겨도 못 푼다).
- Windows가 아니면 available() == False이고, 그때는 예전처럼 평문으로 둔다(설정 파일 권한 0600).
- 포털 아이디·비밀번호는 다루지 않는다(앱은 학교 비밀번호를 받지도 저장하지도 않는다).
"""
from __future__ import annotations

import base64
import ctypes
import sys

_ENTROPY = b"lms-agent/secret/v1"


def available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> "_Blob":
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _take(out: "_Blob") -> bytes:
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def protect(data: bytes) -> bytes:
        src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(src), "LMS Agent", ctypes.byref(ent), None, None,
                                         _UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError("값을 암호화하지 못했습니다")
        return _take(out)

    def unprotect(data: bytes) -> bytes:
        src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                           _UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError("저장된 값을 풀지 못했습니다(다른 PC·계정에서 만든 설정일 수 있음)")
        return _take(out)
else:  # pragma: no cover - Windows 밖에서는 암호화 저장을 쓰지 않는다
    def protect(data: bytes) -> bytes:
        raise OSError("이 운영체제에서는 암호화 저장을 지원하지 않습니다")

    def unprotect(data: bytes) -> bytes:
        raise OSError("이 운영체제에서는 암호화 저장을 지원하지 않습니다")


def encrypt_text(text: str) -> str:
    return base64.b64encode(protect(text.encode("utf-8"))).decode("ascii")


def decrypt_text(blob: str) -> str:
    return unprotect(base64.b64decode(blob)).decode("utf-8")
