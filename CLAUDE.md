# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LMS Agent: Canvas LMS 학기 관리자. 마감이 안 보이는 과제(이메일·종이 제출)의 마감을 공지·메시지·강의계획서에서 찾아 근거와 함께 승인받고, 구글 캘린더에 자동 반영(또는 .ics)하며, 주차·과목·성적(평균·표준편차로 위치 추정)을 보여 주는 배포용 앱. 사용자는 한국어로 대화한다.

## Commands

```bash
python -m pytest                                   # 전체 테스트 (표준 라이브러리 외 의존성은 pytest뿐)
python -m pytest tests/test_extract.py -k midnight # 하나만
PYTHONPATH=src python -m lms_agent demo            # 가짜 Canvas + 가짜 구글로 화면 열기
PYTHONPATH=src python -m lms_agent google-test     # 실제 구글 연결 시험(.env의 OAuth 클라이언트 필요)
python scripts/build_exe.py                        # PyInstaller로 dist/lms-agent.exe
```

## Architecture

- 수집·정리: `canvas.Canvas` → `core.collect`(목록만; 메시지는 바뀐 대화만 본문) → `core.build_items`(출처별 글의 해시가 바뀐 것만 `extract.find_deadlines` → 묶기·Canvas 과제 연결/중복·마감 충돌) → `store.Store.view()`(사용자 결정·근거 사슬 덧붙임).
- 마감을 찾을 글의 출처는 `core.SOURCES`(@source 함수). 새 출처 = 함수 하나. `type:id`는 실행 사이에 같아야 한다.
- 내보내기: `core.calendar_payload(store)` 하나를 .ics(`ics.build_ics`)와 구글(`gsync.desired_events` → `gcal.GoogleCalendar.sync`, 앱이 만든 "LMS 마감" 캘린더만)이 함께 쓴다.
- 서버: `server.py`는 보안 검사(Host, 세션 값 `X-LMS-Agent`)와 HTTP만. API는 `features/*.py`가 `api.router`에 등록한다(`@router.route`, `@router.state_section`, `@router.on("changed")`, `@router.on_init`). 기능 추가 = `features/`에 파일 하나 + `features/__init__.py`의 FEATURES에 이름. 예시는 `api.py` 맨 위.
- 데이터 모양(화면 API, Item, 학기, 구글, 성적)은 `docs/CONTRACTS.md`가 기준. 바꾸면 그 문서와 `web/index.html`을 같이 고친다.
- `mock_canvas.py`·`mock_google.py`는 데모와 테스트가 함께 쓴다. 시나리오를 바꾸면 e2e 기대값도 바뀐다.
- 진행 상황·확인 안 된 것·사용자 결정: `docs/PROGRESS.md`.

## Rules

- 런타임 의존성 추가 금지(누구나 설치 없이 돌아가야 한다). Python 3.9+ 문법.
- 마감 규칙을 고칠 때는 `tests/test_extract.py`에 실패 문장을 먼저 추가하고, 규칙이 바뀌면 `extract.RULES_VERSION`을 올린다(다음 동기화에서 전체를 다시 읽음).
- 토큰·시크릿(Canvas 토큰, 구글 토큰, `.env`)은 로그·오류 메시지·API 응답에 절대 넣지 않는다. `.env`는 git 제외.
- 결정은 보이게·되돌릴 수 있게: 규칙이 찾은 마감·점수는 근거와 함께 후보로 보여 준다. AI 에이전트(설정에서 켰을 때)는 미결정 사안을 직접 처리할 수 있지만, 하네스가 검증한 것만 `decided_by: "ai"`·이유와 함께 반영하고 사용자가 되돌릴 수 있어야 한다(계약 12장).
- `web/characters/ku/`의 GIF는 고려대학교 자산(KU BUDDY)이다 — MIT 대상 아님, 변형 금지(NOTICE.md). 고려대 Canvas 사용자에게만 기본으로 쓰인다.
