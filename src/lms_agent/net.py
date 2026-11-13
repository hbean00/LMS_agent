"""모든 외부 HTTPS 연결(Canvas·LearningX·구글·첨부 다운로드)이 같이 쓰는 보안 설정 — 계약 17장.

- 운영체제가 믿는 인증서를 쓴다: Windows 인증서 저장소(파이썬 기본), macOS 키체인(`security`로 내보내 더함), 리눅스 기본 경로.
- 백신의 'HTTPS 검사'나 학교·회사·기숙사 네트워크 장비가 연결을 가로채 자기 루트 인증서로 다시 서명하는 경우가 있다.
  그 루트 인증서 파일(.cer/.crt/.pem)을 `<데이터 폴더>/certs/`에 넣거나 환경 변수 `LMS_AGENT_CA_FILE`로 알려 주면 함께 믿는다.
- 인증서 검사를 끄는 선택지는 두지 않는다(끄면 Canvas 토큰이 가로채일 수 있다).
"""
from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

ENV_CA = "LMS_AGENT_CA_FILE"
CERT_SUFFIXES = (".pem", ".crt", ".cer", ".der")

_lock = threading.Lock()
_ctx: ssl.SSLContext | None = None
problems: list[str] = []          # 넣었지만 읽지 못한 인증서 파일(화면·오류 문구에 쓴다, 내용은 남기지 않음)


def certs_dir() -> Path:
    from .config import data_dir
    return data_dir() / "certs"


def _extra_files() -> list[Path]:
    files: list[Path] = []
    v = os.environ.get(ENV_CA)
    if v:
        files.append(Path(v))
    try:
        d = certs_dir()
        if d.is_dir():
            files += sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in CERT_SUFFIXES)
    except OSError:
        pass
    return files


def _load_file(ctx: ssl.SSLContext, path: Path) -> None:
    data = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in data:
        ctx.load_verify_locations(cadata=data.decode("ascii", "ignore"))
    else:
        ctx.load_verify_locations(cadata=data)   # DER(.cer) 그대로


def _macos_keychain_pem() -> str:
    """macOS: 시스템·관리자가 설치한 루트 인증서(키체인)를 PEM으로. 파이썬은 키체인을 직접 읽지 않는다."""
    out = []
    for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"):
        try:
            r = subprocess.run(["security", "find-certificate", "-a", "-p", kc], capture_output=True, timeout=8)
            if r.returncode == 0:
                out.append(r.stdout.decode("ascii", "ignore"))
        except (OSError, subprocess.SubprocessError):
            pass
    return "\n".join(out)


def ssl_context() -> ssl.SSLContext:
    """검증을 켠 기본 설정 + 운영체제 인증서 + 사용자가 넣은 루트 인증서. 한 번 만들어 같이 쓴다(reset()으로 다시)."""
    global _ctx
    with _lock:
        if _ctx is not None:
            return _ctx
        ctx = ssl.create_default_context()
        if sys.platform == "darwin":
            pem = _macos_keychain_pem()
            if "BEGIN CERTIFICATE" in pem:
                try:
                    ctx.load_verify_locations(cadata=pem)
                except (ssl.SSLError, ValueError):
                    pass
        problems.clear()
        for p in _extra_files():
            try:
                _load_file(ctx, p)
            except (OSError, ssl.SSLError, ValueError):
                problems.append(p.name)
        _ctx = ctx
        return ctx


def reset() -> None:
    """certs 폴더에 인증서를 넣은 뒤(로그인 다시 시도 등) 새로 읽게 한다."""
    global _ctx
    with _lock:
        _ctx = None


def https_handler() -> urllib.request.HTTPSHandler:
    return urllib.request.HTTPSHandler(context=ssl_context())


def urlopen(req, timeout: float):
    return urllib.request.build_opener(https_handler()).open(req, timeout=timeout)


# ------------------------------------------------------------ 인증서 오류 설명

def is_cert_error(reason) -> bool:
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _tlv(b: bytes, i: int) -> tuple[int, int, int]:
    """DER 하나: (태그, 내용 시작, 내용 끝)."""
    tag = b[i]
    n = b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return tag, i, i + n


def _children(b: bytes, start: int, end: int):
    i = start
    while i < end:
        tag, s, e = _tlv(b, i)
        yield tag, s, e
        i = e


_OIDS = {bytes.fromhex("550403"): "CN", bytes.fromhex("55040a"): "O", bytes.fromhex("55040b"): "OU"}


def issuer_name(der: bytes) -> str | None:
    """인증서(DER)에서 발급자 이름(O·CN). 형식이 이상하면 None. 화면에 '누가 끼어 있는지' 보여 주는 데만 쓴다."""
    try:
        _, s, e = _tlv(der, 0)                       # Certificate
        _, ts, te = _tlv(der, s)                     # TBSCertificate
        fields = list(_children(der, ts, te))
        if fields and fields[0][0] == 0xA0:          # [0] version
            fields = fields[1:]
        _, ns, ne = fields[2]                        # serial, signature, issuer
        parts: dict[str, str] = {}
        for _, ss, se in _children(der, ns, ne):     # SET
            for _, qs, qe in _children(der, ss, se):     # SEQUENCE {OID, value}
                (otag, os_, oe), (vtag, vs, ve) = list(_children(der, qs, qe))[:2]
                key = _OIDS.get(der[os_:oe])
                if key and key not in parts:
                    raw = der[vs:ve]
                    parts[key] = raw.decode("utf-16-be" if vtag == 0x1E else "utf-8", "replace")
        name = " · ".join(v for k in ("O", "CN") for v in [parts.get(k)] if v)
        return name or parts.get("OU")
    except (IndexError, ValueError):
        return None


def peer_issuer(host: str, port: int = 443, timeout: float = 4.0) -> str | None:
    """그 서버가 내미는 인증서의 발급자 이름. 진단용으로 검증 없이 TLS 인사만 하고 끊는다(아무 데이터도 보내지 않음)."""
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        return issuer_name(der) if der else None
    except (OSError, ssl.SSLError, ValueError):
        return None


def cert_help(host: str, reason=None, service: str = "Canvas", port: int = 443) -> str:
    """인증서 검증 실패를 사람이 고칠 수 있는 말로(토큰·개인 정보 없음)."""
    who = peer_issuer(host, port) if host else None
    where = str(certs_dir())
    head = f"{service}({host})의 보안 인증서를 확인하지 못했어요"
    if who:
        head += f" — 인증서를 '{who}'이(가) 발급했어요"
    msg = (f"{head}. 백신의 'HTTPS 검사'(웹 보호)나 학교·회사·기숙사 네트워크 보안 장비가 연결을 검사할 때 생겨요. "
           f"① 그 프로그램에서 HTTPS 검사를 끄거나, ② 그 루트 인증서 파일(.cer/.crt/.pem)을 '{where}' 폴더에 넣고 "
           f"다시 연결해 주세요. 안전을 위해 인증서 검사를 끄는 기능은 없어요.")
    if problems:
        msg += f" (넣어 둔 인증서 중 읽지 못한 파일: {', '.join(problems[:3])})"
    return msg
