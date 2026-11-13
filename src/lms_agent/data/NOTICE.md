# cacert.pem — 공개 루트 인증서 목록

- 내용: Mozilla의 신뢰 루트 인증서 목록(Mozilla CA Certificate Program). `certifi` 패키지가 배포하는 `cacert.pem`(2024.08.30)을 그대로 넣었다(pip에 함께 들어 있는 사본).
- 라이선스: Mozilla Public License 2.0(https://mozilla.org/MPL/2.0/). 이 파일은 MIT 대상이 아니다.
- 쓰는 곳: `src/lms_agent/net.py` — 운영체제 인증서 저장소에 더해 이 목록도 믿는다. 운영체제에 학교 서버의 정상 루트가 아직 없을 때(예: 크롬만 쓰는 Windows) 연결이 실패하지 않게.
- 갱신: 새 버전을 낼 때 최신 `certifi`의 `cacert.pem`으로 바꾼다. 앱 안 업데이트로 사용자에게 함께 전달된다.
