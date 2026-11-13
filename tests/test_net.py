"""공통 HTTPS 설정(계약 17장): 운영체제·사용자 루트 인증서, 인증서 오류를 고칠 수 있는 말로.

베타테스터 오류: "연결 확인 실패: … CERTIFICATE_VERIFY_FAILED … self-signed certificate in certificate chain"
(백신의 HTTPS 검사나 학교·회사 네트워크 장비가 연결을 가로채 자기 루트 인증서로 다시 서명하는 경우).
"""
import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lms_agent import net
from lms_agent.canvas import Canvas, CanvasError


@pytest.fixture(autouse=True)
def fresh_context():
    net.reset()
    yield
    net.reset()


# ------------------------------------------------------------ 발급자 이름(DER)

def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        ln = bytes([n])
    else:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        ln = bytes([0x80 | len(b)]) + b
    return bytes([tag]) + ln + body


def _fake_cert(org: str, cn: str, version: bool = True) -> bytes:
    rdn = lambda oid, v: _tlv(0x31, _tlv(0x30, _tlv(0x06, bytes.fromhex(oid)) + _tlv(0x0C, v.encode())))  # noqa: E731
    issuer = _tlv(0x30, rdn("55040a", org) + rdn("550403", cn))
    tbs = (_tlv(0xA0, _tlv(0x02, b"\x02")) if version else b"") + _tlv(0x02, b"\x01") \
        + _tlv(0x30, _tlv(0x06, bytes.fromhex("2a864886f70d01010b"))) + issuer + _tlv(0x30, b"")
    return _tlv(0x30, _tlv(0x30, tbs) + _tlv(0x30, b"") + _tlv(0x03, b"\x00"))


def test_issuer_name_from_der():
    assert net.issuer_name(_fake_cert("AhnLab", "Safe Transaction Root")) == "AhnLab · Safe Transaction Root"
    assert net.issuer_name(_fake_cert("학교 보안장비", "Inspection CA", version=False)) == "학교 보안장비 · Inspection CA"
    long_cn = "Very Long Corporate Inspection Root Certificate Authority " * 4      # 길이 바이트가 0x81인 경우
    assert net.issuer_name(_fake_cert("Corp", long_cn)).endswith(long_cn)
    assert net.issuer_name(b"\x30\x03\x02\x01") is None and net.issuer_name(b"") is None


def test_cert_error_detection_and_message(monkeypatch):
    err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                          "self-signed certificate in certificate chain (_ssl.c:1000)")
    assert net.is_cert_error(err) and net.is_cert_error("CERTIFICATE_VERIFY_FAILED")
    assert not net.is_cert_error(ConnectionRefusedError())
    monkeypatch.setattr(net, "peer_issuer", lambda host, port=443, timeout=4.0: "Kaspersky · Personal Root")
    msg = net.cert_help("mylms.korea.ac.kr", err)
    assert "mylms.korea.ac.kr" in msg and "Kaspersky · Personal Root" in msg
    assert "HTTPS 검사" in msg and str(net.certs_dir()) in msg and "끄는 기능은 없어요" in msg


def test_unreadable_cert_file_is_reported_not_fatal(monkeypatch):
    d = net.certs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.cer").write_bytes(b"not a certificate")
    net.ssl_context()
    assert net.problems == ["broken.cer"]
    monkeypatch.setattr(net, "peer_issuer", lambda host, port=443, timeout=4.0: None)
    assert "broken.cer" in net.cert_help("example.ac.kr")


# ------------------------------------------------------------ 가로채는 네트워크 흉내(끝에서 끝까지)

OPENSSL = shutil.which("openssl")


def _make_interceptor(d):
    """가짜 '검사 장비' 루트 CA + 그 CA가 서명한 127.0.0.1 서버 인증서."""
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

    def run(*args):
        subprocess.run([OPENSSL, *args], cwd=d, env=env, capture_output=True, check=True, timeout=120)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/O=Test Interceptor/CN=Test Interceptor Root",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=127.0.0.1")
    (d / "ext.cnf").write_text("subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=CA:FALSE\n"
                               "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
                               "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "leaf.pem", "-days", "2", "-extfile", "ext.cnf")
    run("x509", "-in", "ca.pem", "-outform", "der", "-out", "ca.cer")


class _Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):   # 거절된 인사(검증 실패)는 조용히
        pass


class _Me(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"id": 1, "name": "테스트 학생"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def intercepted(tmp_path):
    if not OPENSSL:
        pytest.skip("openssl이 없어 가로채는 네트워크를 흉내 낼 수 없음")
    try:
        _make_interceptor(tmp_path)
    except (subprocess.SubprocessError, OSError):
        pytest.skip("이 openssl로는 시험용 인증서를 만들 수 없음")
    srv = _Quiet(("127.0.0.1", 0), _Me)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield tmp_path, f"https://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_intercepted_login_explains_then_works_with_dropped_in_root(intercepted):
    d, base = intercepted
    with pytest.raises(CanvasError) as e:
        Canvas(base, "secret-token-123").whoami()
    msg = str(e.value)
    assert "보안 인증서" in msg and "Test Interceptor" in msg      # 누가 끼어 있는지
    assert str(net.certs_dir()) in msg and "secret-token-123" not in msg
    certs = net.certs_dir()
    certs.mkdir(parents=True, exist_ok=True)
    shutil.copy(d / "ca.cer", certs / "interceptor.cer")      # 백신·학교가 주는 .cer(DER) 그대로
    net.reset()
    assert Canvas(base, "secret-token-123").whoami()["name"] == "테스트 학생"


def test_env_var_ca_file(intercepted, monkeypatch):
    d, base = intercepted
    monkeypatch.setenv(net.ENV_CA, str(d / "ca.pem"))
    net.reset()
    assert Canvas(base, "t0k3n").whoami()["id"] == 1


def test_verification_is_never_switched_off():
    ctx = net.ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


# ------------------------------------------------------------ 운영체제 목록에 없는 정상 루트(베타테스터 2차 제보)

def test_bundled_public_roots_are_trusted():
    """Windows가 아직 받지 않은 정상 루트(실제: 고려대 LMS의 GlobalSign Root R3)도 앱에 넣은 목록으로 확인한다."""
    assert net._bundled_roots()
    subjects = [dict(x[0] for x in c["subject"]) for c in net.ssl_context().get_ca_certs()]
    assert any(s.get("organizationalUnitName") == "GlobalSign Root CA - R3" for s in subjects)


def test_public_ca_issuer_gets_missing_root_advice_not_interception(monkeypatch):
    monkeypatch.setattr(net, "peer_issuer",
                        lambda host, port=443, timeout=4.0: "GlobalSign nv-sa · GlobalSign GCC R3 DV TLS CA 2020")
    msg = net.cert_help("mylms.korea.ac.kr")
    assert "루트 인증서가 없어서" in msg and "Edge" in msg and "https://mylms.korea.ac.kr" in msg
    assert "HTTPS 검사'(웹 보호)" not in msg                 # 가로채기라고 잘못 말하지 않는다
    assert net.is_public_ca("DigiCert Global G2") and net.is_public_ca("R11 · Let's Encrypt")
    assert not net.is_public_ca("Test Interceptor · Test Interceptor Root") and not net.is_public_ca(None)
